# SPDX-FileCopyrightText: © 2025 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0

import torch
import ttnn
import math
from ttnn.model_preprocessing import (
    preprocess_linear_weight,
    preprocess_linear_bias,
    preprocess_layernorm_parameter,
)
from models.experimental.yolov8s_world.reference.yolov8s_world import (
    Conv,
    C2f,
    SPPF,
    C2fAttn,
    WorldModel,
    WorldDetect,
    ImagePoolingAttn,
)


def determine_num_cores_for_upsample(nhw: int, width: int, max_cores=64) -> int:
    gcd_nhw_width = math.gcd(nhw, width)
    cores = nhw // gcd_nhw_width
    if cores > max_cores:
        for divisor in range(max_cores, 0, -1):
            if nhw % divisor == 0 and (nhw // divisor) % width == 0:
                cores = divisor
                break
    return cores


def get_core_grid_from_num_cores(num_cores: int, grid_rows: int = 8, grid_cols: int = 8):
    rows = num_cores // grid_cols
    assert rows <= grid_rows, "Not enough cores for specified core grid"
    ranges = []
    if rows != 0:
        ranges.append(
            ttnn.CoreRange(
                ttnn.CoreCoord(0, 0),
                ttnn.CoreCoord(grid_rows - 1, rows - 1),
            )
        )
    remainder = num_cores % grid_rows
    if remainder != 0:
        assert rows + 1 <= grid_rows, "Not enough cores for specified core grid"
        ranges.append(
            ttnn.CoreRange(
                ttnn.CoreCoord(0, rows),
                ttnn.CoreCoord(remainder - 1, rows),
            )
        )
    return ttnn.CoreRangeSet({*ranges})


def to_layout(x, layout=ttnn.ROW_MAJOR_LAYOUT):
    if x.get_layout() != layout:
        x = ttnn.to_layout(x, layout)
    return x


def sharded_concat(input_tensors, num_cores=56, dim=3):  # expected input tensors to be in fp16, RM, same (h*w)
    shard_grid = get_core_grid_from_num_cores(num_cores=num_cores)
    in_shard_width = input_tensors[0].shape[-1]
    shard_height = (input_tensors[0].shape[2] + num_cores - 1) // num_cores
    input_sharded_memory_config = ttnn.create_sharded_memory_config_(
        (shard_height, in_shard_width),
        core_grid=shard_grid,
        strategy=ttnn.ShardStrategy.HEIGHT,
        orientation=ttnn.ShardOrientation.ROW_MAJOR,
        use_height_and_width_as_shard_shape=True,
    )
    out_shard_width = 0
    for i in range(len(input_tensors)):
        out_shard_width += input_tensors[i].shape[-1]
        input_tensors[i] = ttnn.to_memory_config(input_tensors[i], input_sharded_memory_config)

    output_sharded_memory_config = ttnn.create_sharded_memory_config_(
        (shard_height, out_shard_width),
        core_grid=shard_grid,
        strategy=ttnn.ShardStrategy.HEIGHT,
        orientation=ttnn.ShardOrientation.ROW_MAJOR,
        use_height_and_width_as_shard_shape=True,
    )
    output = ttnn.concat(input_tensors, dim, memory_config=output_sharded_memory_config)
    return output


def concat(tensors, dim=-1, use_sharded_concat=True):
    if use_sharded_concat:
        processed_tensors = [
            ttnn.to_dtype(to_layout(tensor, ttnn.ROW_MAJOR_LAYOUT), ttnn.bfloat16) for tensor in tensors
        ]
        return sharded_concat(processed_tensors)
    else:
        return ttnn.concat([*tensors], dim=dim, memory_config=ttnn.L1_MEMORY_CONFIG)


def ttnn_decode_bboxes(device, distance, anchor_points, xywh=True, dim=1):
    distance = ttnn.to_layout(distance, ttnn.ROW_MAJOR_LAYOUT)
    # lt, rb = ttnn.split(distance, 2, 1, memory_config=ttnn.L1_MEMORY_CONFIG)  # if done in tile : tt-metal issue #17017

    lt = distance[:, : distance.shape[1] // 2, :]
    rb = distance[:, distance.shape[1] // 2 :, :]

    lt = ttnn.to_layout(lt, ttnn.TILE_LAYOUT)
    rb = ttnn.to_layout(rb, ttnn.TILE_LAYOUT)

    x1y1 = anchor_points - lt
    x2y2 = anchor_points + rb
    if xywh:
        c_xy = x1y1 + x2y2
        c_xy = ttnn.div(c_xy, 2)
        wh = x2y2 - x1y1
        return ttnn.concat([c_xy, wh], 1)


def make_anchors(device, feats, strides, grid_cell_offset=0.5):
    anchor_points, stride_tensor = [], []
    assert feats is not None
    for i, stride in enumerate(strides):
        h, w = feats[i]
        sx = torch.arange(end=w) + grid_cell_offset
        sy = torch.arange(end=h) + grid_cell_offset
        sy, sx = torch.meshgrid(sy, sx)
        anchor_points.append(torch.stack((sx, sy), -1).view(-1, 2))
        stride_tensor.append(torch.full((h * w, 1), stride))

    a = torch.cat(anchor_points).transpose(0, 1).unsqueeze(0)
    b = torch.cat(stride_tensor).transpose(0, 1)

    return (
        ttnn.from_torch(a, dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT, device=device),
        ttnn.from_torch(b, dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT, device=device),
    )


def fold_batch_norm2d_into_conv2d(conv, bn):
    if not bn.track_running_stats:
        raise RuntimeError("BatchNorm2d must have track_running_stats=True to be folded into Conv2d")

    weight = conv.weight
    bias = conv.bias
    running_mean = bn.running_mean
    running_var = bn.running_var
    eps = bn.eps
    scale = bn.weight
    shift = bn.bias
    weight = weight * (scale / torch.sqrt(running_var + eps))[:, None, None, None]
    if bias is not None:
        bias = (bias - running_mean) * (scale / torch.sqrt(running_var + eps)) + shift
    else:
        bias = shift - running_mean * (scale / torch.sqrt(running_var + eps))

    return weight, bias


def create_custom_preprocessor(device):
    def custom_preprocessor(model, name, ttnn_module_args):
        parameters = {}
        if isinstance(model, WorldModel):
            parameters["model"] = {}
            for index, child in enumerate(model.model):
                parameters["model"][index] = {}
                if isinstance(child, Conv):
                    conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child.conv, child.bn)
                    parameters["model"][index]["conv"] = {}
                    parameters["model"][index]["conv"]["weight"] = ttnn.from_torch(conv_weight)
                    parameters["model"][index]["conv"]["bias"] = ttnn.from_torch(
                        conv_bias.reshape(1, 1, 1, -1),
                    )
                elif isinstance(child, C2f):
                    parameters["model"][index]["cv1"] = {}
                    conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child.cv1.conv, child.cv1.bn)
                    parameters["model"][index]["cv1"]["conv"] = {}
                    parameters["model"][index]["cv1"]["conv"]["weight"] = ttnn.from_torch(
                        conv_weight,
                    )
                    parameters["model"][index]["cv1"]["conv"]["bias"] = ttnn.from_torch(
                        conv_bias.reshape(1, 1, 1, -1),
                    )

                    parameters["model"][index]["cv2"] = {}
                    conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child.cv2.conv, child.cv2.bn)
                    parameters["model"][index]["cv2"]["conv"] = {}
                    parameters["model"][index]["cv2"]["conv"]["weight"] = ttnn.from_torch(
                        conv_weight,
                    )
                    parameters["model"][index]["cv2"]["conv"]["bias"] = ttnn.from_torch(
                        conv_bias.reshape(1, 1, 1, -1),
                    )

                    parameters["model"][index]["m"] = {}
                    for index_2, child_2 in enumerate(child.m):
                        parameters["model"][index]["m"][index_2] = {}

                        parameters["model"][index]["m"][index_2]["cv1"] = {}
                        conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child_2.cv1.conv, child_2.cv1.bn)
                        parameters["model"][index]["m"][index_2]["cv1"]["conv"] = {}
                        parameters["model"][index]["m"][index_2]["cv1"]["conv"]["weight"] = ttnn.from_torch(
                            conv_weight,
                        )
                        parameters["model"][index]["m"][index_2]["cv1"]["conv"]["bias"] = ttnn.from_torch(
                            conv_bias.reshape(1, 1, 1, -1),
                        )

                        parameters["model"][index]["m"][index_2]["cv2"] = {}
                        conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child_2.cv2.conv, child_2.cv2.bn)
                        parameters["model"][index]["m"][index_2]["cv2"]["conv"] = {}
                        parameters["model"][index]["m"][index_2]["cv2"]["conv"]["weight"] = ttnn.from_torch(
                            conv_weight,
                        )
                        parameters["model"][index]["m"][index_2]["cv2"]["conv"]["bias"] = ttnn.from_torch(
                            conv_bias.reshape(1, 1, 1, -1),
                        )
                elif isinstance(child, SPPF):
                    parameters["model"][index]["cv1"] = {}
                    conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child.cv1.conv, child.cv1.bn)
                    parameters["model"][index]["cv1"]["conv"] = {}
                    parameters["model"][index]["cv1"]["conv"]["weight"] = ttnn.from_torch(
                        conv_weight,
                    )
                    parameters["model"][index]["cv1"]["conv"]["bias"] = ttnn.from_torch(
                        conv_bias.reshape(1, 1, 1, -1),
                    )

                    parameters["model"][index]["cv2"] = {}
                    conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child.cv2.conv, child.cv2.bn)
                    parameters["model"][index]["cv2"]["conv"] = {}
                    parameters["model"][index]["cv2"]["conv"]["weight"] = ttnn.from_torch(
                        conv_weight,
                    )
                    parameters["model"][index]["cv2"]["conv"]["bias"] = ttnn.from_torch(
                        conv_bias.reshape(1, 1, 1, -1),
                    )
                elif isinstance(child, C2fAttn):
                    parameters["model"][index]["cv1"] = {}
                    conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child.cv1.conv, child.cv1.bn)
                    parameters["model"][index]["cv1"]["conv"] = {}
                    parameters["model"][index]["cv1"]["conv"]["weight"] = ttnn.from_torch(
                        conv_weight,
                    )
                    parameters["model"][index]["cv1"]["conv"]["bias"] = ttnn.from_torch(
                        conv_bias.reshape(1, 1, 1, -1),
                    )

                    parameters["model"][index]["cv2"] = {}
                    conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child.cv2.conv, child.cv2.bn)
                    parameters["model"][index]["cv2"]["conv"] = {}
                    parameters["model"][index]["cv2"]["conv"]["weight"] = ttnn.from_torch(
                        conv_weight,
                    )
                    parameters["model"][index]["cv2"]["conv"]["bias"] = ttnn.from_torch(
                        conv_bias.reshape(1, 1, 1, -1),
                    )

                    parameters["model"][index]["m"] = {}
                    for index_2, child_2 in enumerate(child.m):
                        parameters["model"][index]["m"][index_2] = {}

                        parameters["model"][index]["m"][index_2]["cv1"] = {}
                        conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child_2.cv1.conv, child_2.cv1.bn)
                        parameters["model"][index]["m"][index_2]["cv1"]["conv"] = {}
                        parameters["model"][index]["m"][index_2]["cv1"]["conv"]["weight"] = ttnn.from_torch(
                            conv_weight,
                        )
                        parameters["model"][index]["m"][index_2]["cv1"]["conv"]["bias"] = ttnn.from_torch(
                            conv_bias.reshape(1, 1, 1, -1),
                        )

                        parameters["model"][index]["m"][index_2]["cv2"] = {}
                        conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child_2.cv2.conv, child_2.cv2.bn)
                        parameters["model"][index]["m"][index_2]["cv2"]["conv"] = {}
                        parameters["model"][index]["m"][index_2]["cv2"]["conv"]["weight"] = ttnn.from_torch(
                            conv_weight,
                        )
                        parameters["model"][index]["m"][index_2]["cv2"]["conv"]["bias"] = ttnn.from_torch(
                            conv_bias.reshape(1, 1, 1, -1),
                        )
                    parameters["model"][index]["attn"] = {}
                    if child.attn.ec == None:
                        parameters["model"][index]["attn"]["ec"] = {}
                    else:
                        assert False, "give support for Ec"
                    parameters["model"][index]["attn"]["bias"] = ttnn.from_torch(
                        child.attn.bias, layout=ttnn.TILE_LAYOUT, memory_config=ttnn.L1_MEMORY_CONFIG, device=device
                    )
                    parameters["model"][index]["attn"]["gl"] = {}
                    parameters["model"][index]["attn"]["gl"]["weight"] = preprocess_linear_weight(
                        child.attn.gl.weight, dtype=ttnn.bfloat8_b
                    )
                    parameters["model"][index]["attn"]["gl"]["bias"] = preprocess_linear_bias(
                        child.attn.gl.bias, dtype=ttnn.bfloat8_b
                    )

                    parameters["model"][index]["attn"]["proj_conv"] = {}
                    conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(
                        child.attn.proj_conv.conv, child.attn.proj_conv.bn
                    )
                    parameters["model"][index]["attn"]["proj_conv"]["conv"] = {}
                    parameters["model"][index]["attn"]["proj_conv"]["conv"]["weight"] = ttnn.from_torch(
                        conv_weight,
                    )
                    parameters["model"][index]["attn"]["proj_conv"]["conv"]["bias"] = ttnn.from_torch(
                        conv_bias.reshape(1, 1, 1, -1),
                    )
                elif isinstance(child, ImagePoolingAttn):
                    ##query
                    parameters["model"][index]["query"] = {}
                    # layernorm
                    parameters["model"][index]["query"][0] = {}
                    parameters["model"][index]["query"][0]["weight"] = preprocess_layernorm_parameter(
                        child.query[0].weight, dtype=ttnn.bfloat8_b
                    )
                    parameters["model"][index]["query"][0]["bias"] = preprocess_layernorm_parameter(
                        child.query[0].bias, dtype=ttnn.bfloat8_b
                    )

                    # linear
                    parameters["model"][index]["query"][1] = {}
                    parameters["model"][index]["query"][1]["weight"] = preprocess_linear_weight(
                        child.query[1].weight, dtype=ttnn.bfloat8_b
                    )
                    parameters["model"][index]["query"][1]["bias"] = preprocess_linear_bias(
                        child.query[1].bias, dtype=ttnn.bfloat8_b
                    )

                    ##key
                    parameters["model"][index]["key"] = {}
                    # layernorm
                    parameters["model"][index]["key"][0] = {}
                    parameters["model"][index]["key"][0]["weight"] = preprocess_layernorm_parameter(
                        child.key[0].weight, dtype=ttnn.bfloat8_b
                    )
                    parameters["model"][index]["key"][0]["bias"] = preprocess_layernorm_parameter(
                        child.key[0].bias, dtype=ttnn.bfloat8_b
                    )

                    # linear
                    parameters["model"][index]["key"][1] = {}
                    parameters["model"][index]["key"][1]["weight"] = preprocess_linear_weight(
                        child.key[1].weight, dtype=ttnn.bfloat8_b
                    )
                    parameters["model"][index]["key"][1]["bias"] = preprocess_linear_bias(
                        child.key[1].bias, dtype=ttnn.bfloat8_b
                    )

                    ##value
                    parameters["model"][index]["value"] = {}
                    # layernorm
                    parameters["model"][index]["value"][0] = {}
                    parameters["model"][index]["value"][0]["weight"] = preprocess_layernorm_parameter(
                        child.value[0].weight, dtype=ttnn.bfloat8_b
                    )
                    parameters["model"][index]["value"][0]["bias"] = preprocess_layernorm_parameter(
                        child.value[0].bias, dtype=ttnn.bfloat8_b
                    )

                    # linear
                    parameters["model"][index]["value"][1] = {}
                    parameters["model"][index]["value"][1]["weight"] = preprocess_linear_weight(
                        child.value[1].weight, dtype=ttnn.bfloat8_b
                    )
                    parameters["model"][index]["value"][1]["bias"] = preprocess_linear_bias(
                        child.value[1].bias, dtype=ttnn.bfloat8_b
                    )

                    # proj
                    parameters["model"][index]["proj"] = {}
                    parameters["model"][index]["proj"]["weight"] = preprocess_linear_weight(
                        child.proj.weight, dtype=ttnn.bfloat8_b
                    )
                    parameters["model"][index]["proj"]["bias"] = preprocess_linear_bias(
                        child.proj.bias, dtype=ttnn.bfloat8_b
                    )

                    # projections
                    parameters["model"][index]["projections"] = {}

                    parameters["model"][index]["projections"][0] = {}
                    parameters["model"][index]["projections"][0]["weight"] = ttnn.from_torch(
                        child.projections[0].weight,
                    )
                    parameters["model"][index]["projections"][0]["bias"] = ttnn.from_torch(
                        child.projections[0].bias.reshape(1, 1, 1, -1),
                    )

                    parameters["model"][index]["projections"][1] = {}
                    parameters["model"][index]["projections"][1]["weight"] = ttnn.from_torch(
                        child.projections[1].weight,
                    )
                    parameters["model"][index]["projections"][1]["bias"] = ttnn.from_torch(
                        child.projections[1].bias.reshape(1, 1, 1, -1),
                    )

                    parameters["model"][index]["projections"][2] = {}
                    parameters["model"][index]["projections"][2]["weight"] = ttnn.from_torch(
                        child.projections[2].weight,
                    )
                    parameters["model"][index]["projections"][2]["bias"] = ttnn.from_torch(
                        child.projections[2].bias.reshape(1, 1, 1, -1),
                    )

                elif isinstance(child, WorldDetect):
                    parameters["model"][index]["cv2"] = {}
                    for i_1, child_1 in enumerate(child.cv2):
                        parameters["model"][index]["cv2"][i_1] = {}
                        for i_2, child_2 in enumerate(child_1):
                            parameters["model"][index]["cv2"][i_1][i_2] = {}
                            if i_2 == 2:
                                parameters["model"][index]["cv2"][i_1][i_2]["weight"] = ttnn.from_torch(
                                    child_2.weight,
                                )
                                parameters["model"][index]["cv2"][i_1][i_2]["bias"] = ttnn.from_torch(
                                    child_2.bias.reshape(1, 1, 1, -1),
                                )
                            else:
                                conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child_2.conv, child_2.bn)
                                parameters["model"][index]["cv2"][i_1][i_2]["conv"] = {}
                                parameters["model"][index]["cv2"][i_1][i_2]["conv"]["weight"] = ttnn.from_torch(
                                    conv_weight,
                                )
                                parameters["model"][index]["cv2"][i_1][i_2]["conv"]["bias"] = ttnn.from_torch(
                                    conv_bias.reshape(1, 1, 1, -1),
                                )

                    parameters["model"][index]["cv3"] = {}
                    for i_1, child_1 in enumerate(child.cv3):
                        parameters["model"][index]["cv3"][i_1] = {}
                        for i_2, child_2 in enumerate(child_1):
                            parameters["model"][index]["cv3"][i_1][i_2] = {}
                            if i_2 == 2:
                                parameters["model"][index]["cv3"][i_1][i_2]["weight"] = ttnn.from_torch(
                                    child_2.weight,
                                )
                                parameters["model"][index]["cv3"][i_1][i_2]["bias"] = ttnn.from_torch(
                                    child_2.bias.reshape(1, 1, 1, -1),
                                )
                            else:
                                conv_weight, conv_bias = fold_batch_norm2d_into_conv2d(child_2.conv, child_2.bn)
                                parameters["model"][index]["cv3"][i_1][i_2]["conv"] = {}
                                parameters["model"][index]["cv3"][i_1][i_2]["conv"]["weight"] = ttnn.from_torch(
                                    conv_weight,
                                )
                                parameters["model"][index]["cv3"][i_1][i_2]["conv"]["bias"] = ttnn.from_torch(
                                    conv_bias.reshape(1, 1, 1, -1),
                                )
                    parameters["model"][index]["dfl"] = {}
                    parameters["model"][index]["dfl"]["conv"] = {}
                    parameters["model"][index]["dfl"]["conv"]["weight"] = ttnn.from_torch(
                        child.dfl.conv.weight,
                    )
                    if child.dfl.conv.bias == None:
                        parameters["model"][index]["dfl"]["conv"]["bias"] = None

                    parameters["model"][index]["cv4"] = {}
                    for i_1, child_1 in enumerate(child.cv4):
                        parameters["model"][index]["cv4"][i_1] = {}
                        parameters["model"][index]["cv4"][i_1]["bias"] = ttnn.from_torch(
                            child_1.bias, layout=ttnn.TILE_LAYOUT, memory_config=ttnn.L1_MEMORY_CONFIG, device=device
                        )
                        parameters["model"][index]["cv4"][i_1]["logit_scale"] = ttnn.from_torch(
                            child_1.logit_scale,
                            layout=ttnn.TILE_LAYOUT,
                            memory_config=ttnn.L1_MEMORY_CONFIG,
                            device=device,
                        )

                    strides = [8, 16, 32]

                    feats = [
                        (640 // 8, 640 // 8),
                        (640 // 16, 640 // 16),
                        (640 // 32, 640 // 32),
                    ]  # value depend on res

                    anchors, strides = make_anchors(
                        device, feats, strides
                    )  # Optimization: Processing make anchors outside model run

                    parameters["model"][index]["anchors"] = anchors
                    parameters["model"][index]["strides"] = strides

            parameters["txt_feats"] = ttnn.from_torch(
                model.txt_feats, memory_config=ttnn.L1_MEMORY_CONFIG, layout=ttnn.TILE_LAYOUT, device=device
            )

        return parameters

    return custom_preprocessor
