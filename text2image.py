import argparse
import copy
import math
import os
from typing import Optional

import torch
import scipy
import torch.utils.checkpoint
from omegaconf import OmegaConf
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import ProjectConfiguration, set_seed
from transformers import CLIPTextModel, CLIPTokenizer

from diffusers import (
    AutoencoderKL, UNet2DConditionModel, DDIMScheduler, StableDiffusionPipeline
)
from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion import rescale_noise_cfg
from diffusers.pipelines.stable_diffusion import StableDiffusionPipelineOutput
from sync_tiled_decode import apply_sync_tiled_decode, apply_tiled_processors
from model import ReDilateConvProcessor, inflate_kernels

logger = get_logger(__name__, log_level="INFO")


def parse_args():
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        default="runwayml/stable-diffusion-v1-5",
        help="Path to pretrained model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default=None,
        required=False,
        help="Revision of pretrained model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--validation_prompt", type=str,
        default="a professional photograph of an astronaut riding a horse",
        help="A prompt that is sampled during training for inference."
    )
    parser.add_argument(
        "--cache_dir",
        type=str,
        default=None,
        help="The directory where the downloaded models and datasets will be stored.",
    )
    parser.add_argument("--seed", type=int, default=23, help="A seed for reproducible training.")
    parser.add_argument("--config", type=str, default="./configs/sd1.5_1024x1024_backup.txt")
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default='fp16',
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
            " 1.10.and an Nvidia Ampere GPU.  Default to the value of accelerate config of the current system or the"
            " flag passed with the `accelerate.launch` command. Use this argument to override the accelerate config."
        ),
    )

    args = parser.parse_args()
    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    # Sanity checks
    # if args.dataset_name is None and args.train_data_dir is None:
    #     raise ValueError("Need either a dataset name or a training folder.")

    return args


def pipeline_processor(
        self,
        ndcfg_tau=0,
        dilate_tau=0,
        inflate_tau=0,
        dilate_settings=None,
        inflate_settings=None,
        ndcfg_dilate_settings=None,
        transform=None,
        progressive=False,
):
    @torch.no_grad()
    def forward(
            prompt=None,
            height: Optional[int] = None,
            width: Optional[int] = None,
            num_inference_steps: int = 50,
            guidance_scale: float = 7.5,
            negative_prompt=None,
            num_images_per_prompt: Optional[int] = 1,
            eta: float = 1.0,
            generator=None,
            latents: Optional[torch.FloatTensor] = None,
            prompt_embeds: Optional[torch.FloatTensor] = None,
            negative_prompt_embeds: Optional[torch.FloatTensor] = None,
            output_type: Optional[str] = "pil",
            return_dict: bool = True,
            callback=None,
            callback_steps: int = 1,
            cross_attention_kwargs=None,
            guidance_rescale: float = 0.0,
    ):
        # 0. Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        # 1. Check inputs. Raise error if not correct
        self.check_inputs(
            prompt, height, width, callback_steps, negative_prompt, prompt_embeds, negative_prompt_embeds
        )

        # 2. Define call parameters
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]

        device = self._execution_device
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0

        # 3. Encode input prompt
        text_encoder_lora_scale = (
            cross_attention_kwargs.get("scale", None) if cross_attention_kwargs is not None else None
        )
        prompt_embeds = self._encode_prompt(
            prompt,
            device,
            num_images_per_prompt,
            do_classifier_free_guidance,
            negative_prompt,
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds,
            lora_scale=text_encoder_lora_scale,
        )

        # 4. Prepare timesteps
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps

        # 5. Prepare latent variables
        num_channels_latents = self.unet.in_channels
        latents = self.prepare_latents(
            batch_size * num_images_per_prompt,
            num_channels_latents,
            height,
            width,
            prompt_embeds.dtype,
            device,
            generator,
            latents,
        )

        # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

        # 7. Denoising loop
        num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
        unet_inflate, unet_inflate_vanilla = None, None
        if transform is not None:
            unet_inflate = copy.deepcopy(self.unet)
            if inflate_settings is not None:
                inflate_kernels(unet_inflate, inflate_settings, transform)

        if transform is not None and ndcfg_tau > 0:
            unet_inflate_vanilla = copy.deepcopy(self.unet)
            if inflate_settings is not None:
                inflate_kernels(unet_inflate_vanilla, inflate_settings, transform)
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                # expand the latents if we are doing classifier free guidance
                latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
                latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)

                unet = unet_inflate if i < inflate_tau and transform is not None else self.unet
                backup_forwards = dict()
                for name, module in unet.named_modules():
                    if name in dilate_settings.keys():
                        backup_forwards[name] = module.forward
                        dilate = dilate_settings[name]
                        if progressive:
                            dilate = max(math.ceil(dilate * ((dilate_tau - i) / dilate_tau)), 2)
                        if i < inflate_tau and name in inflate_settings:
                            dilate = dilate / 2
                        # print(f"{name}: {dilate} {i < dilate_tau}")
                        module.forward = ReDilateConvProcessor(
                            module, dilate, mode='bilinear', activate=i < dilate_tau
                        )

                # predict the noise residual
                noise_pred = unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=prompt_embeds,
                    cross_attention_kwargs=cross_attention_kwargs,
                ).sample

                for name, module in unet.named_modules():
                    if name in backup_forwards.keys():
                        module.forward = backup_forwards[name]

                if i < ndcfg_tau:
                    unet = unet_inflate_vanilla if i < inflate_tau and transform is not None else self.unet
                    backup_forwards = dict()
                    for name, module in unet.named_modules():
                        if name in ndcfg_dilate_settings.keys():
                            backup_forwards[name] = module.forward
                            dilate = ndcfg_dilate_settings[name]
                            if progressive:
                                dilate = max(math.ceil(dilate * ((ndcfg_tau - i) / ndcfg_tau)), 2)
                            if i < inflate_tau and name in inflate_settings:
                                dilate = dilate / 2
                            # print(f"{name}: {dilate} {i < dilate_tau}")
                            module.forward = ReDilateConvProcessor(
                                module, dilate, mode='bilinear', activate=i < ndcfg_tau
                            )

                    noise_pred_vanilla = unet(
                        latent_model_input,
                        t,
                        encoder_hidden_states=prompt_embeds,
                        cross_attention_kwargs=cross_attention_kwargs,
                    ).sample

                    for name, module in unet.named_modules():
                        if name in backup_forwards.keys():
                            module.forward = backup_forwards[name]

                # perform guidance
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                    if i < ndcfg_tau:
                        noise_pred_vanilla, _ = noise_pred_vanilla.chunk(2)
                        noise_pred = noise_pred_vanilla + guidance_scale * (noise_pred_text - noise_pred_uncond)
                    else:
                        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

                if do_classifier_free_guidance and guidance_rescale > 0.0:
                    # Based on 3.4. in https://arxiv.org/pdf/2305.08891.pdf
                    noise_pred = rescale_noise_cfg(noise_pred, noise_pred_text, guidance_rescale=guidance_rescale)

                # compute the previous noisy sample x_t -> x_t-1
                latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs, return_dict=False)[0]

                # call the callback, if provided
                if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0):
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        callback(i, t, latents)

        if not output_type == "latent":
            image = self.vae.decode(latents / self.vae.config.scaling_factor, return_dict=False)[0]
            image, has_nsfw_concept = self.run_safety_checker(image, device, prompt_embeds.dtype)
        else:
            image = latents
            has_nsfw_concept = None

        if has_nsfw_concept is None:
            do_denormalize = [True] * image.shape[0]
        else:
            do_denormalize = [not has_nsfw for has_nsfw in has_nsfw_concept]

        image = self.image_processor.postprocess(image, output_type=output_type, do_denormalize=do_denormalize)

        # Offload last model to CPU
        if hasattr(self, "final_offload_hook") and self.final_offload_hook is not None:
            self.final_offload_hook.offload()

        if not return_dict:
            return image, has_nsfw_concept

        return StableDiffusionPipelineOutput(images=image, nsfw_content_detected=has_nsfw_concept)

    return forward


def read_module_list(path):
    with open(path, 'r') as f:
        module_list = f.readlines()
        module_list = [name.strip() for name in module_list]
    return module_list


def read_dilate_settings(path):
    print(f"Reading dilation settings")
    dilate_settings = dict()
    with open(path, 'r') as f:
        raw_lines = f.readlines()
        for raw_line in raw_lines:
            name, dilate = raw_line.split(':')
            dilate_settings[name] = float(dilate)
            print(f"{name} : {dilate_settings[name]}")
    return dilate_settings


def main():
    args = parse_args()
    logging_dir = os.path.join(args.logging_dir)
    config = OmegaConf.load(args.config)

    accelerator_project_config = ProjectConfiguration(logging_dir=logging_dir)
    accelerator = Accelerator(
        mixed_precision=args.mixed_precision,
        project_config=accelerator_project_config,
    )
    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Final inference
    # Load previous pipeline
    tokenizer = CLIPTokenizer.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="tokenizer", revision=args.revision, torch_dtype=weight_dtype
    )
    text_encoder = CLIPTextModel.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="text_encoder", revision=args.revision, torch_dtype=weight_dtype
    )
    vae = AutoencoderKL.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="vae", revision=args.revision, torch_dtype=weight_dtype
    )
    unet = UNet2DConditionModel.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="unet", revision=args.revision, torch_dtype=weight_dtype
    )
    noise_scheduler = DDIMScheduler.from_pretrained(args.pretrained_model_name_or_path, subfolder="scheduler")
    pipeline = StableDiffusionPipeline(
        vae=vae,
        text_encoder=text_encoder,
        tokenizer=tokenizer,
        unet=unet,
        scheduler=noise_scheduler,
        feature_extractor=None,
        safety_checker=None
    )
    pipeline = pipeline.to(accelerator.device)

    dilate_settings = read_dilate_settings(config.dilate_settings) \
        if config.dilate_settings is not None else dict()
    ndcfg_dilate_settings = read_dilate_settings(config.ndcfg_dilate_settings) \
        if config.ndcfg_dilate_settings is not None else dict()
    inflate_settings = read_module_list(config.inflate_settings) \
        if config.inflate_settings is not None else list()
    if config.inflate_transform is not None:
        print(f"Using inflated conv {config.inflate_transform}")
        transform = scipy.io.loadmat(config.inflate_transform)['R']
        transform = torch.tensor(transform, device=accelerator.device)
    else:
        transform = None

    unet.eval()
    total_num = 0
    print(f"Using prompt {args.validation_prompt}")
    if os.path.isfile(args.validation_prompt):
        with open(args.validation_prompt, 'r') as f:
            validation_prompt = f.readlines()
            validation_prompt = [line.strip() for line in validation_prompt]
    else:
        validation_prompt = [args.validation_prompt, ]

    inference_batch_size = config.inference_batch_size
    num_batches = math.ceil(len(validation_prompt) / inference_batch_size)
    pipeline.enable_vae_tiling()
    # apply_sync_tiled_decode(pipeline.vae)
    # apply_tiled_processors(pipeline.vae.decoder)
    for i in range(num_batches):
        output_prompts = validation_prompt[i * inference_batch_size:min(
            (i + 1) * inference_batch_size, len(validation_prompt))]

        for n in range(config.num_iters_per_prompt):
            set_seed(args.seed + n)

            latents = torch.randn(
                (len(output_prompts), 4, config.latent_height, config.latent_width),
                device=accelerator.device, dtype=weight_dtype
            )
            pipeline.forward = pipeline_processor(
                pipeline,
                ndcfg_tau=config.ndcfg_tau,
                dilate_tau=config.dilate_tau,
                inflate_tau=config.inflate_tau,
                dilate_settings=dilate_settings,
                inflate_settings=inflate_settings,
                ndcfg_dilate_settings=ndcfg_dilate_settings,
                transform=transform,
                progressive=config.progressive,
            )
            images = pipeline.forward(
                output_prompts, num_inference_steps=config.num_inference_steps, generator=None, latents=latents).images

            os.makedirs(os.path.join(logging_dir), exist_ok=True)
            for image, prompt in zip(images, output_prompts):
                total_num = total_num + 1
                image.save(fp=os.path.join(logging_dir, f"{total_num}.jpg"))
                with open(os.path.join(logging_dir, f"{total_num}.txt"), 'w') as f:
                    f.writelines([prompt, ])


if __name__ == "__main__":
    main()
