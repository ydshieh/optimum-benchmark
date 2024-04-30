import gc
import os
from collections import OrderedDict
from logging import getLogger
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List

import torch
from datasets import Dataset
from safetensors.torch import save_file
from transformers import (
    AwqConfig,
    BitsAndBytesConfig,
    GPTQConfig,
    Trainer,
    TrainerCallback,
    TrainerState,
    TrainingArguments,
)

from ...import_utils import is_deepspeed_available, is_zentorch_available
from ..base import Backend
from ..peft_utils import apply_peft
from ..transformers_utils import random_init_weights
from .config import PyTorchConfig

if is_deepspeed_available():
    import deepspeed


if is_zentorch_available():
    import zentorch  # type: ignore # noqa: F401


# bachend logger
LOGGER = getLogger("pytorch")


class PyTorchBackend(Backend[PyTorchConfig]):
    NAME = "pytorch"

    def __init__(self, config: PyTorchConfig):
        super().__init__(config)
        self.validate_library()

        if self.config.deepspeed_inference and self.is_quantized:
            raise ValueError("Deepspeed-Inference is not compatible with Transformers quantization")

        # Quantization
        if self.is_quantized:
            LOGGER.info("\t+ Processing quantization config")
            self.process_quantization_config()

        # Threads
        if self.config.inter_op_num_threads is not None:
            LOGGER.info(f"\t+ Setting pytorch inter_op_num_threads({self.config.inter_op_num_threads}))")
            torch.set_num_threads(self.config.inter_op_num_threads)
        if self.config.intra_op_num_threads is not None:
            LOGGER.info(f"\t+ Setting pytorch intra_op_num_threads({self.config.intra_op_num_threads}))")
            torch.set_num_interop_threads(self.config.intra_op_num_threads)

        # Autocast
        if self.config.autocast_enabled:
            LOGGER.info("\t+ Enabling automatic mixed precision")
            torch.set_autocast_enabled(True)

            if self.config.autocast_dtype is not None:
                if self.config.device == "cpu":
                    LOGGER.info(f"\t+ Setting autocast cpu dtype to {self.config.autocast_dtype}")
                    torch.set_autocast_cpu_dtype(getattr(torch, self.config.autocast_dtype))
                elif self.config.device == "cuda":
                    LOGGER.info(f"\t+ Setting autocast gpu dtype to {self.config.autocast_dtype}")
                    torch.set_autocast_gpu_dtype(getattr(torch, self.config.autocast_dtype))
                else:
                    raise ValueError(f"Device {self.config.device} not supported for autocast")

        LOGGER.info("\t+ Creating backend temporary directory")
        self.tmpdir = TemporaryDirectory()

        # Model
        if self.config.no_weights and (self.config.library == "diffusers" or self.config.library == "timm"):
            raise ValueError("Diffusion pipelines and Timm models don't support no weights")
        elif self.config.no_weights:
            LOGGER.info("\t+ Loading model with random weights")
            self.load_model_with_no_weights()
        else:
            LOGGER.info("\t+ Loading model with pretrained weights")
            self.load_model_from_pretrained()

        self.tmpdir.cleanup()

        # KV-Cache
        if self.config.cache_implementation is not None:
            LOGGER.info(f"\t+ Setting cache implementation to {self.config.cache_implementation}")
            self.pretrained_model.generation_config.cache_implementation = self.config.cache_implementation

        # Eval mode
        if self.config.eval_mode and self.config.library != "diffusers":
            LOGGER.info("\t+ Turning on model's eval mode")
            self.pretrained_model.eval()

        # BetterTransformer
        if self.config.to_bettertransformer:
            LOGGER.info("\t+ Enabling BetterTransformer")
            self.pretrained_model.to_bettertransformer()

        # PEFT
        if self.config.peft_type is not None:
            LOGGER.info("\t+ Applying PEFT")
            self.pretrained_model = apply_peft(self.pretrained_model, self.config.peft_type, self.config.peft_config)

        # Torch compile
        if self.config.torch_compile:
            if self.config.library == "diffusers":
                LOGGER.info("\t+ Using torch.compile on unet and vae")
                self.pretrained_model.unet = torch.compile(
                    self.pretrained_model.unet, **self.config.torch_compile_config
                )
                self.pretrained_model.vae.decode = torch.compile(
                    self.pretrained_model.vae.decode, **self.config.torch_compile_config
                )
            else:
                LOGGER.info("\t+ Using torch.compile on model")
                self.pretrained_model.forward = torch.compile(self.pretrained_model.forward, **self.config.torch_compile_config)

                # getting this from `inference/benchmark.py", line 148`
                # `backend.generate(self.inputs, self.config.generate_kwargs)`
                inputs = {'input_ids': torch.tensor([[31460, 13356,  2770, 20443, 12282,   742, 16092]], device='cuda:0'), 'attention_mask': torch.tensor([[1, 1, 1, 1, 1, 1, 1]], device='cuda:0')}
                generate_kwargs = {'num_return_sequences': 1, 'max_new_tokens': 16, 'min_new_tokens': 16, 'temperature': 1.0, 'do_sample': False, 'use_cache': True, 'pad_token_id': 0, 'num_beams': 1}

                # Let's try to generate here
                self.pretrained_model.generate(**inputs, **generate_kwargs)
                print("OK: generate just after compile")

        # DeepSpeed
        if self.config.deepspeed_inference:
            LOGGER.info("\t+ Initializing DeepSpeed Inference Engine")
            self.pretrained_model = deepspeed.init_inference(
                model=self.pretrained_model, config=self.config.deepspeed_inference_config
            )

    def validate_library(self) -> None:
        if self.config.library == "timm":
            LOGGER.info(f"\t+ Using Timm's {self.automodel_class.__name__}")
        elif self.config.library == "diffusers":
            LOGGER.info(f"\t+ Using Diffusers Pipeline {self.automodel_class.__name__}")
        elif self.config.library == "transformers":
            LOGGER.info(f"\t+ Using AutoModel {self.automodel_class.__name__}")
        else:
            raise ValueError(f"Library {self.config.library} not supported")

    def load_model_from_pretrained(self) -> None:
        if self.config.library == "timm":
            LOGGER.info("\t+ Loading Timm model")
            self.pretrained_model = self.automodel_class(model_name=self.config.model)
            if self.config.device != "cpu":
                LOGGER.info(f"\t+ Moving model to device: {self.config.device}")
                self.pretrained_model.to(self.config.device)

        elif self.config.library == "diffusers":
            LOGGER.info("\t+ Loading Diffusion pipeline")
            self.pretrained_model = self.automodel_class.from_pretrained(
                pretrained_model_name_or_path=self.config.model,
                pretrained_model_or_path=self.config.model,
                device_map=self.config.device_map,
                **self.config.hub_kwargs,
                **self.automodel_kwargs,
            )
            if self.config.device_map is None and self.config.device != "cpu":
                LOGGER.info(f"\t+ Moving pipeline to device: {self.config.device}")
                self.pretrained_model.to(self.config.device)

        elif self.is_quantized:
            LOGGER.info(f"\t+ Loading {self.quantization_config.quant_method}-quantized model")
            self.pretrained_model = self.automodel_class.from_pretrained(
                pretrained_model_name_or_path=self.config.model,
                device_map=self.config.device_map or torch.device(self.config.device),
                # quantized models are more compatible with device_map dispatcher than (to(device))
                # using to(device) on quantized models sometimes leaves some layers on cpu or raises
                # an error because the layers are already on the device
                **self.config.hub_kwargs,
                **self.automodel_kwargs,
            )

        elif self.config.device_map is not None:
            LOGGER.info(f"\t+ Loading Transformers model with device map: {self.config.device_map}")
            self.pretrained_model = self.automodel_class.from_pretrained(
                pretrained_model_name_or_path=self.config.model,
                device_map=self.config.device_map,
                **self.config.hub_kwargs,
                **self.automodel_kwargs,
            )

        else:
            LOGGER.info("\t+ Loading Transformers model")
            self.pretrained_model = self.automodel_class.from_pretrained(
                pretrained_model_name_or_path=self.config.model, **self.config.hub_kwargs, **self.automodel_kwargs
            )
            if self.config.device != "cpu":
                LOGGER.info(f"\t+ Moving model to device: {self.config.device}")
                self.pretrained_model.to(self.config.device)

    def create_no_weights_model(self) -> None:
        if self.pretrained_config is None:
            raise ValueError("Can't create no weights model without a pretrained config")

        self.no_weights_model = os.path.join(self.tmpdir.name, "no_weights_model")
        LOGGER.info("\t+ Creating no weights model directory")
        os.makedirs(self.no_weights_model, exist_ok=True)
        LOGGER.info("\t+ Creating no weights model state dict")
        state_dict = torch.nn.Linear(1, 1).state_dict()

        if self.is_exllamav2:
            LOGGER.info("\t+ Adding g_idx to no weights model state dict")
            with torch.device("meta"):
                meta_model = self.automodel_class.from_config(self.pretrained_config)
            for name, module in meta_model.named_modules():
                if hasattr(module, "in_features"):
                    state_dict[name + ".g_idx"] = torch.ones((module.in_features,), dtype=torch.int32)

        LOGGER.info("\t+ Saving no weights model safetensors")
        safetensors = os.path.join(self.no_weights_model, "model.safetensors")
        save_file(tensors=state_dict, filename=safetensors, metadata={"format": "pt"})

        if self.is_quantized:
            LOGGER.info("\t+ Adding quantization config to no weights model's pretrained config")
            self.pretrained_config.quantization_config = self.quantization_config.to_dict()
            # tricking from_pretrained to load the model as if it was quantized

        LOGGER.info("\t+ Saving no weights model pretrained config")
        self.pretrained_config.save_pretrained(save_directory=self.no_weights_model)

    def load_model_with_no_weights(self) -> None:
        LOGGER.info("\t+ Creating no weights model")
        self.create_no_weights_model()

        if self.config.deepspeed_inference:
            with torch.device("meta"):
                # with big models, loading no_weights_model is very slow (randomizing every weight)
                # so we load the model on meta device to speed up the process and then move it to cpu
                LOGGER.info("\t+ Loading Transformers model on meta device for fast initialization")
                self.pretrained_model = self.automodel_class.from_pretrained(
                    pretrained_model_name_or_path=self.config.model,
                    **self.config.hub_kwargs,
                    **self.automodel_kwargs,
                )
            LOGGER.info("\t+ Materializing meta model on CPU to avoid OOM")
            self.pretrained_model.to_empty(device="cpu")
            LOGGER.info("\t+ Tying model weights")
            self.pretrained_model.tie_weights()
        else:
            with random_init_weights():
                original_model, self.config.model = self.config.model, self.no_weights_model
                LOGGER.info("\t+ Loading no weights AutoModel")
                self.load_model_from_pretrained()
                self.config.model = original_model

    def process_quantization_config(self) -> None:
        if self.is_gptq_quantized:
            LOGGER.info("\t+ Processing GPTQ config")
            self.quantization_config = GPTQConfig(
                **dict(getattr(self.pretrained_config, "quantization_config", {}), **self.config.quantization_config)
            )
        elif self.is_awq_quantized:
            LOGGER.info("\t+ Processing AWQ config")
            self.quantization_config = AwqConfig(
                **dict(getattr(self.pretrained_config, "quantization_config", {}), **self.config.quantization_config)
            )
        elif self.is_bnb_quantized:
            LOGGER.info("\t+ Processing BitsAndBytes config")
            self.quantization_config = BitsAndBytesConfig(
                **dict(getattr(self.pretrained_config, "quantization_config", {}), **self.config.quantization_config)
            )
        else:
            raise ValueError(f"Quantization scheme {self.config.quantization_scheme} not recognized")

    @property
    def is_quantized(self) -> bool:
        return self.config.quantization_scheme is not None or (
            hasattr(self.pretrained_config, "quantization_config")
            and self.pretrained_config.quantization_config.get("quant_method", None) is not None
        )

    @property
    def is_bnb_quantized(self) -> bool:
        return self.config.quantization_scheme == "bnb" or (
            hasattr(self.pretrained_config, "quantization_config")
            and self.pretrained_config.quantization_config.get("quant_method", None) == "bnb"
        )

    @property
    def is_gptq_quantized(self) -> bool:
        return self.config.quantization_scheme == "gptq" or (
            hasattr(self.pretrained_config, "quantization_config")
            and self.pretrained_config.quantization_config.get("quant_method", None) == "gptq"
        )

    @property
    def is_awq_quantized(self) -> bool:
        return self.config.quantization_scheme == "awq" or (
            hasattr(self.pretrained_config, "quantization_config")
            and self.pretrained_config.quantization_config.get("quant_method", None) == "awq"
        )

    @property
    def is_exllamav2(self) -> bool:
        return (
            self.is_quantized
            and (self.is_gptq_quantized or self.is_awq_quantized)
            and (
                (
                    hasattr(self.pretrained_config, "quantization_config")
                    and hasattr(self.pretrained_config.quantization_config, "exllama_config")
                    and self.pretrained_config.quantization_config.exllama_config.get("version", None) == 2
                )
                or (
                    "exllama_config" in self.config.quantization_config
                    and self.config.quantization_config["exllama_config"].get("version", None) == 2
                )
            )
        )

    @property
    def automodel_kwargs(self) -> Dict[str, Any]:
        kwargs = {}

        if self.config.torch_dtype is not None:
            kwargs["torch_dtype"] = getattr(torch, self.config.torch_dtype)

        if self.is_quantized:
            kwargs["quantization_config"] = self.quantization_config

        if self.config.attn_implementation is not None:
            kwargs["attn_implementation"] = self.config.attn_implementation

        if self.config.low_cpu_mem_usage is not None:
            kwargs["low_cpu_mem_usage"] = self.config.low_cpu_mem_usage

        if self.config.no_weights:
            # we use our own context manager to load the model with random weights
            kwargs["_fast_init"] = False

        return kwargs

    def prepare_inputs(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        inputs = super().prepare_inputs(inputs)

        if self.config.library == "diffusers":
            inputs = {"prompt": inputs["prompt"]}
        elif self.config.library == "timm":
            inputs = {"x": inputs["pixel_values"].to(self.config.device)}
        else:
            for key, value in inputs.items():
                inputs[key] = value.to(self.config.device)

        return inputs

    @torch.inference_mode()
    def forward(self, inputs: Dict[str, Any], kwargs: Dict[str, Any]) -> OrderedDict:
        return self.pretrained_model.forward(**inputs, **kwargs)

    @torch.inference_mode()
    def prefill(self, inputs: Dict[str, Any], kwargs: Dict[str, Any]) -> OrderedDict:
        return self.pretrained_model.generate(**inputs, **kwargs)

    @torch.inference_mode()
    def generate(self, inputs: Dict[str, Any], kwargs: Dict[str, Any]) -> OrderedDict:
        return self.pretrained_model.generate(**inputs, **kwargs)

    @torch.inference_mode()
    def call(self, inputs: Dict[str, Any], kwargs: Dict[str, Any]) -> OrderedDict:
        return self.pretrained_model(**inputs, **kwargs)

    def train(
        self,
        training_dataset: Dataset,
        training_arguments: Dict[str, Any],
        training_callbacks: List[TrainerCallback],
        training_data_collator: Callable[[List[Dict[str, Any]]], Dict[str, Any]],
    ) -> TrainerState:
        LOGGER.info(f"\t+ Wrapping training arguments with {TrainingArguments.__name__}")
        training_arguments = TrainingArguments(**training_arguments)
        LOGGER.info(f"\t+ Wrapping model with {Trainer.__name__}")
        trainer = Trainer(
            args=training_arguments,
            model=self.pretrained_model,
            callbacks=training_callbacks,
            train_dataset=training_dataset,
            data_collator=training_data_collator,
        )
        LOGGER.info("\t+ Starting training")
        trainer.train()
        LOGGER.info("\t+ Finished training")

    def seed(self):
        super().seed()
        torch.manual_seed(self.config.seed)
        torch.cuda.manual_seed_all(self.config.seed)

    def clean(self) -> None:
        if hasattr(self, "tmpdir"):
            LOGGER.info("\t+ Cleaning backend temporary directory")
            self.tmpdir.cleanup()

        if hasattr(self, "pretrained_model"):
            LOGGER.info("\t+ Deleting pretrained model")
            del self.pretrained_model
            gc.collect()

        if self.config.device == "cuda":
            LOGGER.info("\t+ Emptying CUDA cache")
            torch.cuda.empty_cache()
