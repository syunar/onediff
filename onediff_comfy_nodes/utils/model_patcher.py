import copy

import torch
import comfy


def state_dict_hook(module, state_dict, prefix, local_metadata):
    new_state_dict = type(state_dict)()
    for k, v in state_dict.items():
        # diffusion_model._deployable_module_model._torch_module.out.2.weight => diffusion_model.out.2.weight
        if k.startswith("diffusion_model._deployable_module_model"):
            x = k.split(".")
            new_k = ".".join(x[:1] + x[3:])
            new_state_dict[new_k] = v
        else:
            new_state_dict[k] = v
    return new_state_dict


class OneFlowSpeedUpModelPatcher(comfy.model_patcher.ModelPatcher):
    def __init__(
        self,
        model,
        load_device,
        offload_device,
        size=0,
        current_device=None,
        weight_inplace_update=False,
        *,
        use_graph=None,
    ):
        from onediff.infer_compiler import oneflow_compile
        from onediff.infer_compiler.with_oneflow_compile import DeployableModule

        self.weight_inplace_update = weight_inplace_update
        self.object_patches = {}
        self.object_patches_backup = {}
        self.size = size
        self.model = copy.copy(model)
        self.model.__dict__["_modules"] = copy.copy(model.__dict__["_modules"])
        if isinstance(self.model.diffusion_model, DeployableModule):
            self.model.__dict__["_modules"][
                "diffusion_model"
            ] = self.model.diffusion_model
        else:
            self.model.__dict__["_modules"]["diffusion_model"] = oneflow_compile(
                self.model.diffusion_model, use_graph=use_graph
            )
        self.model._register_state_dict_hook(state_dict_hook)
        self.patches = {}
        self.backup = {}
        self.model_options = {"transformer_options": {}}
        self.model_size()
        self.load_device = load_device
        self.offload_device = offload_device
        if current_device is None:
            self.current_device = self.offload_device
        else:
            self.current_device = current_device

    def clone(self):
        n = OneFlowSpeedUpModelPatcher(
            self.model,
            self.load_device,
            self.offload_device,
            self.size,
            self.current_device,
            weight_inplace_update=self.weight_inplace_update,
        )
        n.patches = {}
        for k in self.patches:
            n.patches[k] = self.patches[k][:]

        n.object_patches = self.object_patches.copy()
        n.model_options = copy.deepcopy(self.model_options)
        n.model_keys = self.model_keys
        return n

    def add_patches(self, patches, strength_patch=1.0, strength_model=1.0):
        from comfy.ldm.modules.attention import CrossAttention

        is_diffusers_quant_available = False
        try:
            import diffusers_quant

            is_diffusers_quant_available = True
        except:
            pass

        torch_model = self.model.diffusion_model._deployable_module_model._torch_module
        for name, module in torch_model.named_modules():
            if isinstance(module, CrossAttention) and hasattr(module, "to_qkv"):
                # TODO(): support bias
                assert module.to_qkv.bias is None
                to_q_w_name = f"diffusion_model.{name}.to_q.weight"
                to_k_w_name = f"diffusion_model.{name}.to_k.weight"
                to_v_w_name = f"diffusion_model.{name}.to_v.weight"
                if (
                    to_q_w_name not in patches
                    or to_k_w_name not in patches
                    or to_v_w_name not in patches
                ):
                    continue
                to_q_w = patches[to_q_w_name]
                to_k_w = patches[to_k_w_name]
                to_v_w = patches[to_v_w_name]
                assert to_q_w[2] == to_k_w[2] and to_q_w[2] == to_v_w[2]
                to_qkv_w_name = f"diffusion_model.{name}.to_qkv.weight"

                dim_head = module.to_qkv.out_features // module.heads // 3
                patches[to_qkv_w_name] = tuple(
                    [
                        torch.stack((to_q_w[0], to_k_w[0], to_v_w[0]), dim=0).reshape(
                            3, module.heads, dim_head, -1
                        ),  # (3, H, K, (BM))
                        torch.stack((to_q_w[1], to_k_w[1], to_v_w[1]), dim=0),
                    ]
                    + list(to_q_w[2:])
                )
            if is_diffusers_quant_available:
                if isinstance(
                    module, diffusers_quant.DynamicQuantLinearModule
                ) or isinstance(module, diffusers_quant.DynamicQuantConvModule):
                    w_name = f"diffusion_model.{name}.weight"
                    if w_name in patches:
                        patches[w_name] = tuple(list(patches[w_name]) + [module])
                    b_name = f"diffusion_model.{name}.bias"
                    if b_name in patches:
                        patches[b_name] = tuple(list(patches[b_name]) + [module])

        p = set()
        for k in patches:
            if k in self.model_keys:
                p.add(k)
                current_patches = self.patches.get(k, [])
                current_patches.append((strength_patch, patches[k], strength_model))
                self.patches[k] = current_patches

        return list(p)

    def calculate_weight(self, patches, weight, key):
        is_diffusers_quant_available = False
        try:
            import diffusers_quant

            is_diffusers_quant_available = True
        except:
            pass

        for p in patches:
            alpha = p[0]
            v = p[1]
            strength_model = p[2]

            is_rewrite_qkv = (
                True if (len(v) == 4 or len(v) == 5) and "to_qkv" in key else False
            )
            is_quant = False
            if (
                is_diffusers_quant_available
                and len(v) == 5
                and (
                    isinstance(v[4], diffusers_quant.DynamicQuantLinearModule)
                    or isinstance(v[4], diffusers_quant.DynamicQuantConvModule)
                )
            ):
                is_quant = True
                org_weight_scale = (
                    v[4]
                    .weight_scale.reshape(
                        [-1] + [1 for _ in range(len(weight.shape) - 1)]
                    )
                    .to(weight.device)
                )
                weight = weight.to(torch.float32) * org_weight_scale

            if strength_model != 1.0:
                weight *= strength_model

            if isinstance(v, list):
                v = (self.calculate_weight(v[1:], v[0].clone(), key),)

            if len(v) == 1:
                w1 = v[0]
                if alpha != 0.0:
                    if w1.shape != weight.shape:
                        print(
                            "WARNING SHAPE MISMATCH {} WEIGHT NOT MERGED {} != {}".format(
                                key, w1.shape, weight.shape
                            )
                        )
                    else:
                        weight += alpha * comfy.model_management.cast_to_device(
                            w1, weight.device, weight.dtype
                        )
            elif len(v) == 4 or is_rewrite_qkv or is_quant:  # lora/locon
                mat1 = comfy.model_management.cast_to_device(
                    v[0], weight.device, torch.float32
                )
                mat2 = comfy.model_management.cast_to_device(
                    v[1], weight.device, torch.float32
                )
                if v[2] is not None:
                    if is_rewrite_qkv:
                        alpha *= v[2] / mat2.shape[1]
                    else:
                        alpha *= v[2] / mat2.shape[0]
                if v[3] is not None:
                    # TODO(): support rewrite qkv
                    assert not is_rewrite_qkv

                    # locon mid weights, hopefully the math is fine because I didn't properly test it
                    mat3 = comfy.model_management.cast_to_device(
                        v[3], weight.device, torch.float32
                    )
                    final_shape = [
                        mat2.shape[1],
                        mat2.shape[0],
                        mat3.shape[2],
                        mat3.shape[3],
                    ]
                    mat2 = (
                        torch.mm(
                            mat2.transpose(0, 1).flatten(start_dim=1),
                            mat3.transpose(0, 1).flatten(start_dim=1),
                        )
                        .reshape(final_shape)
                        .transpose(0, 1)
                    )
                try:
                    if is_rewrite_qkv:
                        heads = mat1.shape[1]
                        qkv_lora = alpha * torch.bmm(
                            mat1.reshape(3, -1, mat2.shape[1]),
                            mat2.flatten(start_dim=2),
                        )
                        qkv_lora = qkv_lora.reshape(
                            3, heads, -1, qkv_lora.shape[2]
                        )  # reshape to (3, H, K, (BM))
                        qkv_lora = qkv_lora.permute(
                            1, 0, 2, 3
                        )  # permute to (H, 3, K, (BM))
                        weight += qkv_lora.reshape(weight.shape).type(weight.dtype)
                    else:
                        weight += (
                            (
                                alpha
                                * torch.mm(
                                    mat1.flatten(start_dim=1), mat2.flatten(start_dim=1)
                                )
                            )
                            .reshape(weight.shape)
                            .type(weight.dtype)
                        )
                    if is_quant:
                        weight_max = torch.max(
                            weight.reshape(weight.shape[0], -1), dim=1
                        )[0].reshape([-1] + [1 for _ in range(len(weight.shape) - 1)])
                        weight_scale = torch.abs(weight_max) / 127
                        weight = torch.clamp(
                            torch.round(weight / weight_scale), -127, 127
                        ).to(weight.dtype)
                        weight_acc = (weight * weight_scale).sum(
                            dim=[d for d in range(1, len(weight.shape))]
                        )
                        weight_scale = weight_scale.reshape(v[4].weight_scale.shape).to(
                            v[4].weight_scale.dtype
                        )
                        weight_acc = weight_acc.reshape(v[4].weight_acc.shape).to(
                            v[4].weight_acc.dtype
                        )
                        v[4].weight_scale.copy_(weight_scale)
                        v[4].weight_acc.copy_(weight_acc)
                except Exception as e:
                    print("ERROR", key, e)
            elif len(v) == 8:  # lokr
                w1 = v[0]
                w2 = v[1]
                w1_a = v[3]
                w1_b = v[4]
                w2_a = v[5]
                w2_b = v[6]
                t2 = v[7]
                dim = None

                if w1 is None:
                    dim = w1_b.shape[0]
                    w1 = torch.mm(
                        comfy.model_management.cast_to_device(
                            w1_a, weight.device, torch.float32
                        ),
                        comfy.model_management.cast_to_device(
                            w1_b, weight.device, torch.float32
                        ),
                    )
                else:
                    w1 = comfy.model_management.cast_to_device(
                        w1, weight.device, torch.float32
                    )

                if w2 is None:
                    dim = w2_b.shape[0]
                    if t2 is None:
                        w2 = torch.mm(
                            comfy.model_management.cast_to_device(
                                w2_a, weight.device, torch.float32
                            ),
                            comfy.model_management.cast_to_device(
                                w2_b, weight.device, torch.float32
                            ),
                        )
                    else:
                        w2 = torch.einsum(
                            "i j k l, j r, i p -> p r k l",
                            comfy.model_management.cast_to_device(
                                t2, weight.device, torch.float32
                            ),
                            comfy.model_management.cast_to_device(
                                w2_b, weight.device, torch.float32
                            ),
                            comfy.model_management.cast_to_device(
                                w2_a, weight.device, torch.float32
                            ),
                        )
                else:
                    w2 = comfy.model_management.cast_to_device(
                        w2, weight.device, torch.float32
                    )

                if len(w2.shape) == 4:
                    w1 = w1.unsqueeze(2).unsqueeze(2)
                if v[2] is not None and dim is not None:
                    alpha *= v[2] / dim

                try:
                    weight += alpha * torch.kron(w1, w2).reshape(weight.shape).type(
                        weight.dtype
                    )
                except Exception as e:
                    print("ERROR", key, e)
            else:  # loha
                w1a = v[0]
                w1b = v[1]
                if v[2] is not None:
                    alpha *= v[2] / w1b.shape[0]
                w2a = v[3]
                w2b = v[4]
                if v[5] is not None:  # cp decomposition
                    t1 = v[5]
                    t2 = v[6]
                    m1 = torch.einsum(
                        "i j k l, j r, i p -> p r k l",
                        comfy.model_management.cast_to_device(
                            t1, weight.device, torch.float32
                        ),
                        comfy.model_management.cast_to_device(
                            w1b, weight.device, torch.float32
                        ),
                        comfy.model_management.cast_to_device(
                            w1a, weight.device, torch.float32
                        ),
                    )

                    m2 = torch.einsum(
                        "i j k l, j r, i p -> p r k l",
                        comfy.model_management.cast_to_device(
                            t2, weight.device, torch.float32
                        ),
                        comfy.model_management.cast_to_device(
                            w2b, weight.device, torch.float32
                        ),
                        comfy.model_management.cast_to_device(
                            w2a, weight.device, torch.float32
                        ),
                    )
                else:
                    m1 = torch.mm(
                        comfy.model_management.cast_to_device(
                            w1a, weight.device, torch.float32
                        ),
                        comfy.model_management.cast_to_device(
                            w1b, weight.device, torch.float32
                        ),
                    )
                    m2 = torch.mm(
                        comfy.model_management.cast_to_device(
                            w2a, weight.device, torch.float32
                        ),
                        comfy.model_management.cast_to_device(
                            w2b, weight.device, torch.float32
                        ),
                    )

                try:
                    weight += (alpha * m1 * m2).reshape(weight.shape).type(weight.dtype)
                except Exception as e:
                    print("ERROR", key, e)

        return weight