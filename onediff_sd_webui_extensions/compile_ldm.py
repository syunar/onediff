import math
import oneflow as flow
from onediff.infer_compiler import oneflow_compile, register

from ldm.modules.attention import BasicTransformerBlock, CrossAttention
from ldm.modules.diffusionmodules.openaimodel import ResBlock, UNetModel
from ldm.modules.diffusionmodules.util import GroupNorm32
from sd_webui_onediff_utils import (
    CrossAttentionOflow,
    GroupNorm32Oflow,
    timestep_embedding,
)

__all__ = ["compile_ldm_unet"]


# https://github.com/Stability-AI/stablediffusion/blob/b4bdae9916f628461e1e4edbc62aafedebb9f7ed/ldm/modules/diffusionmodules/openaimodel.py#L775
class UNetModelOflow(flow.nn.Module):
    def forward(self, x, timesteps=None, context=None, y=None, **kwargs):
        assert (y is not None) == (
            self.num_classes is not None
        ), "must specify y if and only if the model is class-conditional"
        hs = []
        t_emb = timestep_embedding(timesteps, self.model_channels).half()
        emb = self.time_embed(t_emb)
        x = x.half()
        context = context.half()
        if self.num_classes is not None:
            assert y.shape[0] == x.shape[0]
            emb = emb + self.label_emb(y)
        h = x
        for module in self.input_blocks:
            h = module(h, emb, context)
            hs.append(h)
        h = self.middle_block(h, emb, context)
        for module in self.output_blocks:
            h = flow.cat([h, hs.pop()], dim=1)
            h = module(h, emb, context)
        if self.predict_codebook_ids:
            return self.id_predictor(h)
        else:
            return self.out(h)


torch2oflow_class_map = {
    CrossAttention: CrossAttentionOflow,
    GroupNorm32: GroupNorm32Oflow,
    UNetModel: UNetModelOflow,
}
register(package_names=["ldm"], torch2oflow_class_map=torch2oflow_class_map)


def compile_ldm_unet(sd_model, *, use_graph=True, options={}):
    unet_model = sd_model.model.diffusion_model
    if not isinstance(unet_model, UNetModel):
        return
    for module in unet_model.modules():
        if isinstance(module, BasicTransformerBlock):
            module.checkpoint = False
        if isinstance(module, ResBlock):
            module.use_checkpoint = False
    return oneflow_compile(unet_model, use_graph=use_graph, options=options)
