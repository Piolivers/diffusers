import gc
import unittest

import torch

from diffusers import StableDiffusionXLInstructPix2PixPipeline
from diffusers.utils.testing_utils import (
    enable_full_determinism,
    require_torch_gpu,
    slow,
)


enable_full_determinism()


@slow
@require_torch_gpu
class StableDiffusionXLInstructPix2PixPipeline(unittest.TestCase):
    pipeline_class = StableDiffusionXLInstructPix2PixPipeline
    ckpt_path = "https://huggingface.co/stabilityai/cosxl/blob/main/cosxl_edit.safetensors"
    original_config = None
    repo_id = "diffusers/sdxl-instructpix2pix-768"

    def setUp(self):
        super().setUp()
        gc.collect()
        torch.cuda.empty_cache()

    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def get_inputs(self, device, generator_device="cpu", dtype=torch.float32, seed=0):
        generator = torch.Generator(device=generator_device).manual_seed(seed)
        inputs = {
            "prompt": "a fantasy landscape, concept art, high resolution",
            "generator": generator,
            "num_inference_steps": 2,
            "strength": 0.75,
            "guidance_scale": 7.5,
            "output_type": "np",
        }
        return inputs

    def test_single_file_setting_cosxl_edit(self):
        # Default is PNDM for this checkpoint
        pipe = self.pipeline_class.from_single_file(self.ckpt_path, is_cosxl_edit=True)
        assert pipe.is_cosxl_edit is True
