# coding=utf-8
# Copyright 2024 HuggingFace Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import CLIPTextConfig, CLIPTextModelWithProjection, CLIPTokenizer

from diffusers import DDPMWuerstchenScheduler, StableCascadePriorPipeline
from diffusers.loaders import AttnProcsLayers
from diffusers.models.attention_processor import (
    LoRAAttnProcessor,
    LoRAAttnProcessor2_0,
)
from diffusers.pipelines.stable_cascade import StableCascadeUnet
from diffusers.utils.import_utils import is_peft_available
from diffusers.utils.testing_utils import enable_full_determinism, require_peft_backend, skip_mps, torch_device


if is_peft_available():
    from peft import LoraConfig
    from peft.tuners.tuners_utils import BaseTunerLayer

from ..test_pipelines_common import PipelineTesterMixin


enable_full_determinism()


def create_prior_lora_layers(unet: nn.Module):
    lora_attn_procs = {}
    for name in unet.attn_processors.keys():
        lora_attn_processor_class = (
            LoRAAttnProcessor2_0 if hasattr(F, "scaled_dot_product_attention") else LoRAAttnProcessor
        )
        lora_attn_procs[name] = lora_attn_processor_class(
            hidden_size=unet.config.c,
        )
    unet_lora_layers = AttnProcsLayers(lora_attn_procs)
    return lora_attn_procs, unet_lora_layers


class StableCascadePriorPipelineFastTests(PipelineTesterMixin, unittest.TestCase):
    pipeline_class = StableCascadePriorPipeline
    params = ["prompt"]
    batch_params = ["prompt", "negative_prompt"]
    required_optional_params = [
        "num_images_per_prompt",
        "generator",
        "num_inference_steps",
        "latents",
        "negative_prompt",
        "guidance_scale",
        "output_type",
        "return_dict",
    ]
    test_xformers_attention = False
    callback_cfg_params = ["text_encoder_hidden_states"]

    @property
    def text_embedder_hidden_size(self):
        return 32

    @property
    def time_input_dim(self):
        return 32

    @property
    def block_out_channels_0(self):
        return self.time_input_dim

    @property
    def time_embed_dim(self):
        return self.time_input_dim * 4

    @property
    def dummy_tokenizer(self):
        tokenizer = CLIPTokenizer.from_pretrained("hf-internal-testing/tiny-random-clip")
        return tokenizer

    @property
    def dummy_text_encoder(self):
        torch.manual_seed(0)
        config = CLIPTextConfig(
            bos_token_id=0,
            eos_token_id=2,
            hidden_size=self.text_embedder_hidden_size,
            projection_dim=self.text_embedder_hidden_size,
            intermediate_size=37,
            layer_norm_eps=1e-05,
            num_attention_heads=4,
            num_hidden_layers=5,
            pad_token_id=1,
            vocab_size=1000,
        )
        return CLIPTextModelWithProjection(config).eval()

    @property
    def dummy_prior(self):
        torch.manual_seed(0)

        model_kwargs = {
            "c_cond": 128,
            "c_hidden": [128, 128],
            "nhead": [2, 2],
            "blocks": [[1, 1], [1, 1]],
            "switch_level": [False],
            "c_clip_img": 768,
            "c_clip_text": self.text_embedder_hidden_size,
            "c_clip_text_pooled": self.text_embedder_hidden_size,
        }

        model = StableCascadeUnet(**model_kwargs)
        return model.eval()

    def get_dummy_components(self):
        prior = self.dummy_prior
        text_encoder = self.dummy_text_encoder
        tokenizer = self.dummy_tokenizer

        scheduler = DDPMWuerstchenScheduler()

        components = {
            "prior": prior,
            "text_encoder": text_encoder,
            "tokenizer": tokenizer,
            "scheduler": scheduler,
            "feature_extractor": None,
            "image_encoder": None,
        }

        return components

    def get_dummy_inputs(self, device, seed=0):
        if str(device).startswith("mps"):
            generator = torch.manual_seed(seed)
        else:
            generator = torch.Generator(device=device).manual_seed(seed)
        inputs = {
            "prompt": "horse",
            "generator": generator,
            "guidance_scale": 4.0,
            "num_inference_steps": 2,
            "output_type": "np",
        }
        return inputs

    def test_wuerstchen_prior(self):
        device = "cpu"

        components = self.get_dummy_components()

        pipe = self.pipeline_class(**components)
        pipe = pipe.to(device)

        pipe.set_progress_bar_config(disable=None)

        output = pipe(**self.get_dummy_inputs(device))
        image = output.image_embeddings

        image_from_tuple = pipe(**self.get_dummy_inputs(device), return_dict=False)[0]

        image_slice = image[0, 0, 0, -10:]
        image_from_tuple_slice = image_from_tuple[0, 0, 0, -10:]
        assert image.shape == (1, 16, 24, 24)

        expected_slice = np.array(
            [
                96.139565,
                -20.213179,
                -116.40341,
                -191.57129,
                39.350136,
                74.80767,
                39.782352,
                -184.67352,
                -46.426907,
                168.41783,
            ]
        )

        assert np.abs(image_slice.flatten() - expected_slice).max() < 5e-2
        assert np.abs(image_from_tuple_slice.flatten() - expected_slice).max() < 5e-2

    @skip_mps
    def test_inference_batch_single_identical(self):
        self._test_inference_batch_single_identical(expected_max_diff=2e-1)

    @skip_mps
    def test_attention_slicing_forward_pass(self):
        test_max_difference = torch_device == "cpu"
        test_mean_pixel_difference = False

        self._test_attention_slicing_forward_pass(
            test_max_difference=test_max_difference,
            test_mean_pixel_difference=test_mean_pixel_difference,
        )

    @unittest.skip(reason="fp16 not supported")
    def test_float16_inference(self):
        super().test_float16_inference()

    def check_if_lora_correctly_set(self, model) -> bool:
        """
        Checks if the LoRA layers are correctly set with peft
        """
        for module in model.modules():
            if isinstance(module, BaseTunerLayer):
                return True
        return False

    def get_lora_components(self):
        prior = self.dummy_prior

        prior_lora_config = LoraConfig(
            r=4, lora_alpha=4, target_modules=["to_q", "to_k", "to_v", "to_out.0"], init_lora_weights=False
        )

        prior_lora_attn_procs, prior_lora_layers = create_prior_lora_layers(prior)

        lora_components = {
            "prior_lora_layers": prior_lora_layers,
            "prior_lora_attn_procs": prior_lora_attn_procs,
        }

        return prior, prior_lora_config, lora_components

    @require_peft_backend
    @unittest.skip(reason="no lora support for now")
    def test_inference_with_prior_lora(self):
        _, prior_lora_config, _ = self.get_lora_components()
        device = "cpu"

        components = self.get_dummy_components()

        pipe = self.pipeline_class(**components)
        pipe = pipe.to(device)

        pipe.set_progress_bar_config(disable=None)

        output_no_lora = pipe(**self.get_dummy_inputs(device))
        image_embed = output_no_lora.image_embeddings
        self.assertTrue(image_embed.shape == (1, 16, 24, 24))

        pipe.prior.add_adapter(prior_lora_config)
        self.assertTrue(self.check_if_lora_correctly_set(pipe.prior), "Lora not correctly set in prior")

        output_lora = pipe(**self.get_dummy_inputs(device))
        lora_image_embed = output_lora.image_embeddings

        self.assertTrue(image_embed.shape == lora_image_embed.shape)
