# This file is autogenerated by the command `make fix-copies`, do not edit.
# flake8: noqa

from ..utils import DummyObject, requires_backends


class StableDiffusionPipeline(metaclass=DummyObject):
    _backends = ["transformers", "onnx"]

    def __init__(self, *args, **kwargs):
        requires_backends(self, ["transformers", "onnx"])
