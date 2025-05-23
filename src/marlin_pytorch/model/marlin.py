import os.path
import shutil
from collections import deque
from pathlib import Path
from typing import Generator, Optional
from urllib.request import urlretrieve

import cv2
import ffmpeg
import numpy as np
import torch
from einops import rearrange
from torch import Tensor
from torch.nn import Linear, Module

from ..config import resolve_config, Downloadable
from ..face_detector import FaceXZooFaceDetector

from .decoder import MarlinDecoder
from .encoder import MarlinEncoder
from ..util import read_video, padding_video, DownloadProgressBar


class Marlin(Module):

    def __init__(self,
        img_size: int,
        patch_size: int,
        n_frames: int,
        encoder_embed_dim: int,
        encoder_depth: int,
        encoder_num_heads: int,
        decoder_embed_dim: int,
        decoder_depth: int,
        decoder_num_heads: int,
        mlp_ratio: float,
        qkv_bias: bool,
        qk_scale: Optional[float],
        drop_rate: float,
        attn_drop_rate: float,
        norm_layer: str,
        init_values: float,
        tubelet_size: int,
        as_feature_extractor: bool = True,
        ffmpeg_dir: str = None
    ):
        super().__init__()
        self.ffmpeg_dir = ffmpeg_dir
        self.encoder = MarlinEncoder(
            img_size=img_size,
            patch_size=patch_size,
            n_frames=n_frames,
            embed_dim=encoder_embed_dim,
            depth=encoder_depth,
            num_heads=encoder_num_heads,
            mlp_ratio=mlp_ratio,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            drop_rate=drop_rate,
            attn_drop_rate=attn_drop_rate,
            norm_layer=norm_layer,
            init_values=init_values,
            tubelet_size=tubelet_size
        )
        self.as_feature_extractor = as_feature_extractor
        self.clip_frames = n_frames
        if as_feature_extractor:
            self.enc_dec_proj = None
            self.decoder = None
        else:
            self.decoder = MarlinDecoder(
                img_size=img_size,
                patch_size=patch_size,
                embed_dim=decoder_embed_dim,
                depth=decoder_depth,
                num_heads=decoder_num_heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop_rate=drop_rate,
                attn_drop_rate=attn_drop_rate,
                norm_layer=norm_layer,
                init_values=init_values,
                tubelet_size=tubelet_size
            )

            self.enc_dec_proj = Linear(encoder_embed_dim, decoder_embed_dim, bias=False)

    def forward(self, x: Tensor, mask: Tensor = None) -> Tensor:
        if self.as_feature_extractor:
            raise RuntimeError("For feature extraction, please use `extract_features` or `extract_video`.")
        else:
            assert mask is not None
            x = self.encoder(x, mask)
            x = self.enc_dec_proj(x)
            x = self.decoder(x, mask)
        return x

    @property
    def device(self):
        return self.encoder.norm.weight.device

    def extract_features(self, x: Tensor, keep_seq: bool = True):
        """Extract features for one video clip (v)"""
        if self.training:
            return self.encoder.extract_features(x, seq_mean_pool=not keep_seq)
        else:
            with torch.no_grad():
                return self.encoder.extract_features(x, seq_mean_pool=not keep_seq)

    def _crop_face(self, v: Tensor) -> Tensor:
        # use face sdk to crop face
        # v: (1, C, T, H, W)
        v = (rearrange(v, "b c t h w -> (b t) h w c").cpu().numpy() * 255).astype(np.uint8)
        face_frames = []
        for i in range(v.shape[0]):
            # crop_face result: (H, W, C)
            face_frames.append(torch.from_numpy(FaceXZooFaceDetector.crop_face(v[i])[0]))

        faces = torch.stack(face_frames)  # (T, H, W, C)
        return rearrange(faces, "(b t) h w c -> b c t h w", b=1).to(self.device) / 255

    @torch.no_grad()
    def extract_video(self, video_path: str, crop_face: bool = False, sample_rate: int = 2,
        stride: int = 16,
        reduction: str = "none",
        keep_seq: bool = False,
        detector_device: Optional[str] = None
    ) -> Tensor:
        self.eval()
        features = []
        for v in self._load_video(video_path, sample_rate, stride):
            # v: (1, C, T, H, W)
            if crop_face:
                if not FaceXZooFaceDetector.inited:
                    Path(".marlin").mkdir(exist_ok=True)
                    FaceXZooFaceDetector.init(
                        face_sdk_path=FaceXZooFaceDetector.install(os.path.join(".marlin", "FaceXZoo")),
                        device=detector_device or self.device
                    )
                v = self._crop_face(v)
            assert v.shape[3:] == (224, 224)
            features.append(self.extract_features(v, keep_seq=keep_seq))

        features = torch.cat(features)  # (N, 768)

        if reduction == "mean":
            return features.mean(dim=0)
        elif reduction == "max":
            return features.max(dim=0)[0]

        return features

    def _load_video(self, video_path: str, sample_rate: int, stride: int) -> Generator[Tensor, None, None]:
        ffmpeg_cmd = 'ffprobe'
        if self.ffmpeg_dir is not None:
            ffmpeg_cmd = os.path.join(self.ffmpeg_dir, ffmpeg_cmd)
        probe = ffmpeg.probe(video_path, cmd=ffmpeg_cmd)
        total_frames = int(probe["streams"][0]["nb_frames"])
        if total_frames <= self.clip_frames:
            video = read_video(video_path, channel_first=True) / 255  # (T, C, H, W)
            # pad frames to 16
            v = padding_video(video, self.clip_frames, "same")  # (T, C, H, W)
            assert v.shape[0] == self.clip_frames
            yield v.permute(1, 0, 2, 3).unsqueeze(0).to(self.device)
        elif total_frames <= self.clip_frames * sample_rate:
            video = read_video(video_path, channel_first=True) / 255  # (T, C, H, W)
            # use first 16 frames
            if video.shape[0] < self.clip_frames:
                # double-check the number of frames, see https://github.com/pytorch/vision/issues/2490
                v = padding_video(video, self.clip_frames, "same")  # (T, C, H, W)
            v = video[:self.clip_frames]
            yield v.permute(1, 0, 2, 3).unsqueeze(0).to(self.device)
        else:
            # extract features based on sliding window
            cap = cv2.VideoCapture(video_path)
            deq = deque(maxlen=self.clip_frames)

            clip_start_indexes = list(range(0, total_frames - self.clip_frames * sample_rate, stride * sample_rate))
            clip_end_indexes = [i + self.clip_frames * sample_rate - 1 for i in clip_start_indexes]

            clip_end_indexes = list(range(self.clip_frames // 2, total_frames + self.clip_frames // 2))

            current_index = -1
            last_frame = None
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                current_index += 1
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frame = torch.from_numpy(frame).permute(2, 0, 1) / 255  # (C, H, W)
                last_frame = frame

                if current_index == 0:
                    for _ in range(self.clip_frames // 2 - 1):
                        deq.append(frame)

                for _ in range(sample_rate - 1):
                    cap.read()
                    current_index += 1

                deq.append(frame)
                if current_index in clip_end_indexes:
                    v = torch.stack(list(deq))  # (T, C, H, W)
                    yield v.permute(1, 0, 2, 3).unsqueeze(0).to(self.device)

            cap.release()

            for _ in range(self.clip_frames // 2):
                deq.append(last_frame)
                v = torch.stack(list(deq))  # (T, C, H, W)
                yield v.permute(1, 0, 2, 3).unsqueeze(0).to(self.device)

    @classmethod
    def from_file(cls, model_name: str, path: str) -> "Marlin":
        if path.endswith(".pt"):
            state_dict = torch.load(path, map_location="cpu")
        elif path.endswith(".ckpt"):
            state_dict = torch.load(path, map_location="cpu")["state_dict"]

            discriminator_keys = [k for k in state_dict.keys() if k.startswith("discriminator")]
            for key in discriminator_keys:
                del state_dict[key]
        else:
            raise ValueError(f"Unsupported file type: {path.split('.')[-1]}")
        # determine if the checkpoint is full model or encoder only.
        for key in state_dict.keys():
            if key.startswith("decoder."):
                as_feature_extractor = False
                break
        else:
            as_feature_extractor = True

        config = resolve_config(model_name)
        model = cls(
            img_size=config.img_size,
            patch_size=config.patch_size,
            n_frames=config.n_frames,
            encoder_embed_dim=config.encoder_embed_dim,
            encoder_depth=config.encoder_depth,
            encoder_num_heads=config.encoder_num_heads,
            decoder_embed_dim=config.decoder_embed_dim,
            decoder_depth=config.decoder_depth,
            decoder_num_heads=config.decoder_num_heads,
            mlp_ratio=config.mlp_ratio,
            qkv_bias=config.qkv_bias,
            qk_scale=config.qk_scale,
            drop_rate=config.drop_rate,
            attn_drop_rate=config.attn_drop_rate,
            norm_layer=config.norm_layer,
            init_values=config.init_values,
            tubelet_size=config.tubelet_size,
            as_feature_extractor=as_feature_extractor
        )
        model.load_state_dict(state_dict)
        return model

    @classmethod
    def from_online(cls, model_name: str, full_model: bool = False) -> "Marlin":
        config = resolve_config(model_name)
        if not isinstance(config, Downloadable):
            raise ValueError(f"Model {model_name} is not downloadable.")

        url = config.full_model_url if full_model else config.encoder_model_url
        path = Path(".marlin")
        path.mkdir(exist_ok=True)
        file = path / f"{model_name}.{'full' if full_model else 'encoder'}.pt"
        if not file.exists():
            with DownloadProgressBar(unit="B", unit_scale=True, miniters=1, desc="Downloading Marlin model") as pb:
                urlretrieve(url, filename=file, reporthook=pb.update_to)
        return cls.from_file(model_name, str(file))

    @classmethod
    def clean_cache(cls, verbose: bool = True) -> None:
        path = Path(".marlin")
        if path.exists():
            shutil.rmtree(path)
            if verbose:
                print("Marlin checkpoints cache cleaned.")
