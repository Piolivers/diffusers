# coding=utf-8
# Copyright 2022 HuggingFace Inc.
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

import gc
import random
import tempfile
import unittest

import numpy as np
import torch

from diffusers import (
    AutoencoderKL,
    DDIMScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
    StableDiffusionDepth2ImgPipeline,
    UNet2DConditionModel,
)
from diffusers.utils import floats_tensor, load_image, load_numpy, slow, torch_device
from diffusers.utils.import_utils import is_accelerate_available
from diffusers.utils.testing_utils import require_torch_gpu
from PIL import Image
from transformers import (
    CLIPTextConfig,
    CLIPTextModel,
    CLIPTokenizer,
    DPTConfig,
    DPTFeatureExtractor,
    DPTForDepthEstimation,
)

from ...test_pipelines_common import PipelineTesterMixin


torch.backends.cuda.matmul.allow_tf32 = False


class StableDiffusionImg2ImgPipelineFastTests(PipelineTesterMixin, unittest.TestCase):
    pipeline_class = StableDiffusionDepth2ImgPipeline
    test_save_load_optional_components = False

    def get_dummy_components(self):
        torch.manual_seed(0)
        unet = UNet2DConditionModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            sample_size=32,
            in_channels=5,
            out_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            up_block_types=("CrossAttnUpBlock2D", "UpBlock2D"),
            cross_attention_dim=32,
            attention_head_dim=(2, 4, 8, 8),
            use_linear_projection=True,
        )
        scheduler = PNDMScheduler(skip_prk_steps=True)
        torch.manual_seed(0)
        vae = AutoencoderKL(
            block_out_channels=[32, 64],
            in_channels=3,
            out_channels=3,
            down_block_types=["DownEncoderBlock2D", "DownEncoderBlock2D"],
            up_block_types=["UpDecoderBlock2D", "UpDecoderBlock2D"],
            latent_channels=4,
        )
        torch.manual_seed(0)
        text_encoder_config = CLIPTextConfig(
            bos_token_id=0,
            eos_token_id=2,
            hidden_size=32,
            intermediate_size=37,
            layer_norm_eps=1e-05,
            num_attention_heads=4,
            num_hidden_layers=5,
            pad_token_id=1,
            vocab_size=1000,
        )
        text_encoder = CLIPTextModel(text_encoder_config)
        tokenizer = CLIPTokenizer.from_pretrained("hf-internal-testing/tiny-random-clip")

        backbone_config = {
            "global_padding": "same",
            "layer_type": "bottleneck",
            "depths": [3, 4, 9],
            "out_features": ["stage1", "stage2", "stage3"],
            "embedding_dynamic_padding": True,
            "hidden_sizes": [96, 192, 384, 768],
            "num_groups": 2,
        }
        depth_estimator_config = DPTConfig(
            image_size=32,
            patch_size=16,
            num_channels=3,
            hidden_size=32,
            num_hidden_layers=4,
            backbone_out_indices=(0, 1, 2, 3),
            num_attention_heads=4,
            intermediate_size=37,
            hidden_act="gelu",
            hidden_dropout_prob=0.1,
            attention_probs_dropout_prob=0.1,
            is_decoder=False,
            initializer_range=0.02,
            is_hybrid=True,
            backbone_config=backbone_config,
            backbone_featmap_shape=[1, 384, 24, 24],
        )
        depth_estimator = DPTForDepthEstimation(depth_estimator_config)
        feature_extractor = DPTFeatureExtractor.from_pretrained(
            "hf-internal-testing/tiny-random-DPTForDepthEstimation"
        )

        components = {
            "unet": unet,
            "scheduler": scheduler,
            "vae": vae,
            "text_encoder": text_encoder,
            "tokenizer": tokenizer,
            "depth_estimator": depth_estimator,
            "feature_extractor": feature_extractor,
        }
        return components

    def get_dummy_inputs(self, device, seed=0):
        image = floats_tensor((1, 3, 32, 32), rng=random.Random(seed)).to(device)
        if str(device).startswith("mps"):
            generator = torch.manual_seed(seed)
        else:
            generator = torch.Generator(device=device).manual_seed(seed)
        inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "image": image,
            "generator": generator,
            "num_inference_steps": 2,
            "guidance_scale": 6.0,
            "output_type": "numpy",
        }
        return inputs

    def test_save_load_local(self):
        if torch_device == "mps":
            # FIXME: inconsistent outputs on MPS
            return

        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(torch_device)
        output = pipe(**inputs)[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            pipe.save_pretrained(tmpdir)
            pipe_loaded = self.pipeline_class.from_pretrained(tmpdir)
            pipe_loaded.to(torch_device)
            pipe_loaded.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(torch_device)
        output_loaded = pipe_loaded(**inputs)[0]

        max_diff = np.abs(output - output_loaded).max()
        self.assertLess(max_diff, 3e-5)

    @unittest.skipIf(torch_device != "cuda", reason="float16 requires CUDA")
    def test_save_load_float16(self):
        components = self.get_dummy_components()
        for name, module in components.items():
            if hasattr(module, "half"):
                components[name] = module.to(torch_device).half()
        pipe = self.pipeline_class(**components)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(torch_device)
        output = pipe(**inputs)[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            pipe.save_pretrained(tmpdir)
            pipe_loaded = self.pipeline_class.from_pretrained(tmpdir, torch_dtype=torch.float16)
            pipe_loaded.to(torch_device)
            pipe_loaded.set_progress_bar_config(disable=None)

        for name, component in pipe_loaded.components.items():
            if hasattr(component, "dtype"):
                self.assertTrue(
                    component.dtype == torch.float16,
                    f"`{name}.dtype` switched from `float16` to {component.dtype} after loading.",
                )

        inputs = self.get_dummy_inputs(torch_device)
        output_loaded = pipe_loaded(**inputs)[0]

        max_diff = np.abs(output - output_loaded).max()
        self.assertLess(max_diff, 2e-2, "The output of the fp16 pipeline changed after saving and loading.")

    @unittest.skipIf(torch_device != "cuda", reason="float16 requires CUDA")
    def test_float16_inference(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        for name, module in components.items():
            if hasattr(module, "half"):
                components[name] = module.half()
        pipe_fp16 = self.pipeline_class(**components)
        pipe_fp16.to(torch_device)
        pipe_fp16.set_progress_bar_config(disable=None)

        output = pipe(**self.get_dummy_inputs(torch_device))[0]
        output_fp16 = pipe_fp16(**self.get_dummy_inputs(torch_device))[0]

        max_diff = np.abs(output - output_fp16).max()
        self.assertLess(max_diff, 1.3e-2, "The outputs of the fp16 and fp32 pipelines are too different.")

    @unittest.skipIf(
        torch_device != "cuda" or not is_accelerate_available(),
        reason="CPU offload is only available with CUDA and `accelerate` installed",
    )
    def test_cpu_offload_forward_pass(self):
        if not self.test_cpu_offload:
            return

        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(torch_device)
        output_without_offload = pipe(**inputs)[0]

        pipe.enable_sequential_cpu_offload()
        inputs = self.get_dummy_inputs(torch_device)
        output_with_offload = pipe(**inputs)[0]

        max_diff = np.abs(output_with_offload - output_without_offload).max()
        self.assertLess(max_diff, 3e-5, "CPU offloading should not affect the inference results")

    def test_dict_tuple_outputs_equivalent(self):
        if torch_device == "mps":
            return

        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        # Warmup pass when using mps (see #372)
        if torch_device == "mps":
            _ = pipe(**self.get_dummy_inputs(torch_device))

        output = pipe(**self.get_dummy_inputs(torch_device))[0]
        output_tuple = pipe(**self.get_dummy_inputs(torch_device), return_dict=False)[0]

        max_diff = np.abs(output - output_tuple).max()
        self.assertLess(max_diff, 3e-5)

    def test_num_inference_steps_consistent(self):
        if torch_device == "mps":
            return
        super().test_num_inference_steps_consistent()

    def test_progress_bar(self):
        if torch_device == "mps":
            return
        super().test_progress_bar()

    def test_stable_diffusion_img2img_default_case(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()
        sd_pipe = StableDiffusionDepth2ImgPipeline(**components)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1]

        assert image.shape == (1, 32, 32, 3)
        if torch_device == "mps":
            expected_slice = np.array([0.6071, 0.5035, 0.4378, 0.5776, 0.5753, 0.4316, 0.4513, 0.5263, 0.4546])
        else:
            expected_slice = np.array([0.6907, 0.5135, 0.4688, 0.5169, 0.5738, 0.4600, 0.4435, 0.5640, 0.4653])
        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-3

    def test_stable_diffusion_img2img_negative_prompt(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()
        sd_pipe = StableDiffusionDepth2ImgPipeline(**components)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        negative_prompt = "french fries"
        output = sd_pipe(**inputs, negative_prompt=negative_prompt)
        image = output.images
        image_slice = image[0, -3:, -3:, -1]

        assert image.shape == (1, 32, 32, 3)
        if torch_device == "mps":
            expected_slice = np.array([0.5825, 0.5135, 0.4095, 0.5452, 0.6059, 0.4211, 0.3994, 0.5177, 0.4335])
        else:
            expected_slice = np.array([0.755, 0.521, 0.473, 0.554, 0.629, 0.442, 0.440, 0.582, 0.449])
        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-3

    def test_stable_diffusion_img2img_multiple_init_images(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()
        sd_pipe = StableDiffusionDepth2ImgPipeline(**components)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        inputs["prompt"] = [inputs["prompt"]] * 2
        inputs["image"] = inputs["image"].repeat(2, 1, 1, 1)
        image = sd_pipe(**inputs).images
        image_slice = image[-1, -3:, -3:, -1]

        assert image.shape == (2, 32, 32, 3)

        if torch_device == "mps":
            expected_slice = np.array([0.6501, 0.5150, 0.4939, 0.6688, 0.5437, 0.5758, 0.5115, 0.4406, 0.4551])
        else:
            expected_slice = np.array([0.6475, 0.6302, 0.5627, 0.5222, 0.4318, 0.5489, 0.5079, 0.4419, 0.4494])
        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-3

    def test_stable_diffusion_img2img_num_images_per_prompt(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()
        sd_pipe = StableDiffusionDepth2ImgPipeline(**components)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        # test num_images_per_prompt=1 (default)
        inputs = self.get_dummy_inputs(device)
        images = sd_pipe(**inputs).images

        assert images.shape == (1, 32, 32, 3)

        # test num_images_per_prompt=1 (default) for batch of prompts
        batch_size = 2
        inputs = self.get_dummy_inputs(device)
        inputs["prompt"] = [inputs["prompt"]] * batch_size
        images = sd_pipe(**inputs).images

        assert images.shape == (batch_size, 32, 32, 3)

        # test num_images_per_prompt for single prompt
        num_images_per_prompt = 2
        inputs = self.get_dummy_inputs(device)
        images = sd_pipe(**inputs, num_images_per_prompt=num_images_per_prompt).images

        assert images.shape == (num_images_per_prompt, 32, 32, 3)

        # test num_images_per_prompt for batch of prompts
        batch_size = 2
        inputs = self.get_dummy_inputs(device)
        inputs["prompt"] = [inputs["prompt"]] * batch_size
        images = sd_pipe(**inputs, num_images_per_prompt=num_images_per_prompt).images

        assert images.shape == (batch_size * num_images_per_prompt, 32, 32, 3)

    def test_stable_diffusion_img2img_pil(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()
        sd_pipe = StableDiffusionDepth2ImgPipeline(**components)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)

        inputs["image"] = Image.fromarray(inputs["image"][0].permute(1, 2, 0).numpy().astype(np.uint8))
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1]

        if torch_device == "mps":
            expected_slice = np.array([0.53232, 0.47015, 0.40868, 0.45651, 0.4891, 0.4668, 0.4287, 0.48822, 0.47439])
        else:
            expected_slice = np.array([0.6853, 0.3740, 0.4856, 0.7130, 0.7402, 0.5535, 0.4828, 0.6182, 0.5053])
        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-3


@slow
@require_torch_gpu
class StableDiffusionImg2ImgPipelineIntegrationTests(unittest.TestCase):
    def tearDown(self):
        # clean up the VRAM after each test
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def test_stable_diffusion_img2img_pipeline_default(self):
        init_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/depth2img/two_cats.png"
        )
        expected_image = load_numpy(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/depth2img/two_cats.npy"
        )

        model_id = "stabilityai/stable-diffusion-2-depth"
        pipe = StableDiffusionDepth2ImgPipeline.from_pretrained(model_id)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()

        prompt = "two tigers"

        generator = torch.Generator(device=torch_device).manual_seed(0)
        output = pipe(
            prompt=prompt,
            image=init_image,
            strength=0.75,
            generator=generator,
            output_type="np",
        )
        image = output.images[0]

        assert image.shape == (480, 640, 3)
        # img2img is flaky across GPUs even in fp32, so using MAE here
        assert np.abs(expected_image - image).max() < 1e-3

    def test_stable_diffusion_img2img_pipeline_k_lms(self):
        init_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/depth2img/two_cats.png"
        )
        expected_image = load_numpy(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/depth2img/two_cats_k_lms.npy"
        )

        model_id = "stabilityai/stable-diffusion-2-depth"
        lms = LMSDiscreteScheduler.from_pretrained(model_id, subfolder="scheduler")
        pipe = StableDiffusionDepth2ImgPipeline.from_pretrained(model_id, scheduler=lms)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()

        prompt = "two tigers"

        generator = torch.Generator(device=torch_device).manual_seed(0)
        output = pipe(
            prompt=prompt,
            image=init_image,
            strength=0.75,
            generator=generator,
            output_type="np",
        )
        image = output.images[0]

        assert image.shape == (480, 640, 3)
        assert np.abs(expected_image - image).max() < 1e-3

    def test_stable_diffusion_img2img_pipeline_ddim(self):
        init_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/depth2img/two_cats.png"
        )
        expected_image = load_numpy(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/depth2img/two_cats_ddim.npy"
        )

        model_id = "stabilityai/stable-diffusion-2-depth"
        ddim = DDIMScheduler.from_pretrained(model_id, subfolder="scheduler")
        pipe = StableDiffusionDepth2ImgPipeline.from_pretrained(model_id, scheduler=ddim)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()

        prompt = "two tigers"

        generator = torch.Generator(device=torch_device).manual_seed(0)
        output = pipe(
            prompt=prompt,
            image=init_image,
            strength=0.75,
            generator=generator,
            output_type="np",
        )
        image = output.images[0]

        assert image.shape == (480, 640, 3)
        assert np.abs(expected_image - image).max() < 1e-3

    def test_stable_diffusion_img2img_intermediate_state(self):
        number_of_steps = 0

        def test_callback_fn(step: int, timestep: int, latents: torch.FloatTensor) -> None:
            test_callback_fn.has_been_called = True
            nonlocal number_of_steps
            number_of_steps += 1
            if step == 1:
                latents = latents.detach().cpu().numpy()
                assert latents.shape == (1, 4, 60, 80)
                latents_slice = latents[0, -3:, -3:, -1]
                expected_slice = np.array(
                    [-0.7825, 0.5786, -0.9125, -0.9885, -1.0071, 2.7126, -0.8490, 0.3776, -0.0791]
                )
                assert np.abs(latents_slice.flatten() - expected_slice).max() < 1e-3
            elif step == 37:
                latents = latents.detach().cpu().numpy()
                assert latents.shape == (1, 4, 60, 80)
                latents_slice = latents[0, -3:, -3:, -1]
                expected_slice = np.array(
                    [-0.6110, -0.2347, -0.5115, -1.1383, -1.4755, -0.5970, -0.9050, -0.7199, -0.8417]
                )
                assert np.abs(latents_slice.flatten() - expected_slice).max() < 1e-2

        test_callback_fn.has_been_called = False

        init_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/depth2img/two_cats.png"
        )

        pipe = StableDiffusionDepth2ImgPipeline.from_pretrained("stabilityai/stable-diffusion-2-depth")
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()

        prompt = "two tigers"

        generator = torch.Generator(device=torch_device).manual_seed(0)
        pipe(
            prompt=prompt,
            image=init_image,
            strength=0.75,
            num_inference_steps=50,
            generator=generator,
            callback=test_callback_fn,
            callback_steps=1,
        )
        assert test_callback_fn.has_been_called
        assert number_of_steps == 37

    def test_stable_diffusion_pipeline_with_sequential_cpu_offloading(self):
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        init_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main"
            "/img2img/sketch-mountains-input.jpg"
        )
        init_image = init_image.resize((768, 512))

        model_id = "stabilityai/stable-diffusion-2-depth"
        lms = LMSDiscreteScheduler.from_pretrained(model_id, subfolder="scheduler")
        pipe = StableDiffusionDepth2ImgPipeline.from_pretrained(
            model_id, scheduler=lms, safety_checker=None, revision="fp16", torch_dtype=torch.float16
        )
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing(1)
        pipe.enable_sequential_cpu_offload()

        prompt = "A fantasy landscape, trending on artstation"

        generator = torch.Generator(device=torch_device).manual_seed(0)
        _ = pipe(
            prompt=prompt,
            image=init_image,
            strength=0.75,
            guidance_scale=7.5,
            generator=generator,
            output_type="np",
            num_inference_steps=5,
        )

        mem_bytes = torch.cuda.max_memory_allocated()
        # make sure that less than 2.9 GB is allocated
        assert mem_bytes < 2.9 * 10**9
