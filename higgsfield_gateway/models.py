from __future__ import annotations

import base64
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MODELS = {
    "seedance_2_5_draft": {"minimum_duration": 4, "maximum_duration": 15,
                         "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"], "first_frame": False},
    "wan3_0": {"minimum_duration": 2, "maximum_duration": 30,
               "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1"], "first_frame": False},
    "grok_video_v15_lite": {"minimum_duration": 2, "maximum_duration": 15,
                           "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1"], "first_frame": True},
    "seedance_2_5": {"minimum_duration": 4, "maximum_duration": 15,
                     "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"], "first_frame": False},
}


def provider_model(model: str) -> str:
    return "seedance_2_5" if model == "seedance_2_5_draft" else model


class LimitUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    binding_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=9223372036854775806)
    credit_limit: str = Field(pattern=r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?$")
    submission_limit: int = Field(ge=0, le=2147483647)
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")


class SourceImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content_base64: str = Field(min_length=1, max_length=14 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    format: Literal["png", "jpeg", "webp"]

    def content(self):
        data = base64.b64decode(self.content_base64, validate=True)
        if not 0 < len(data) <= 10 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("source_integrity_failed")
        return data


class ReferenceMedia(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0, le=128 * 1024 * 1024)
    kind: Literal["image", "video"]
    format: Literal["png", "jpeg", "webp", "mp4"]

    @model_validator(mode="after")
    def media_type(self):
        if (self.kind == "video") != (self.format == "mp4") or (self.kind == "image" and self.size > 10 * 1024 * 1024):
            raise ValueError("invalid_reference_media")
        return self


class Generation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["grok_video_v15_lite", "seedance_2_5", "seedance_2_5_draft", "wan3_0"]
    prompt: str = Field(min_length=1, max_length=10000)
    duration_seconds: int
    aspect_ratio: str = "16:9"
    resolution: Literal["480p", "720p"] = "720p"
    mode: Literal["text_to_video", "image_to_video", "reference_to_video"] = "text_to_video"
    source: SourceImage | None = None
    references: list[ReferenceMedia] = Field(default_factory=list, max_length=5)

    @field_validator("prompt")
    @classmethod
    def prompt_valid(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("invalid_prompt")
        return value

    @model_validator(mode="after")
    def supported(self):
        model = MODELS[self.model]
        if not model["minimum_duration"] <= self.duration_seconds <= model["maximum_duration"]:
            raise ValueError("unsupported_duration")
        if self.aspect_ratio not in model["ratios"]:
            raise ValueError("unsupported_aspect_ratio")
        if self.model == "seedance_2_5" and self.resolution != "720p":
            raise ValueError("unsupported_resolution")
        if self.model == "seedance_2_5_draft":
            if self.resolution != "480p":
                raise ValueError("unsupported_resolution")
            if self.mode != "reference_to_video":
                raise ValueError("reference_to_video_required")
        if (self.source is not None) != (self.mode == "image_to_video"):
            raise ValueError("mode_reference_mismatch")
        if self.source and not model["first_frame"]:
            raise ValueError("unsupported_references")
        if bool(self.references) != (self.mode == "reference_to_video"):
            raise ValueError("mode_reference_mismatch")
        if self.references:
            if self.model not in {"wan3_0", "seedance_2_5_draft"} or self.source:
                raise ValueError("unsupported_references")
            if sum(r.kind == "image" for r in self.references) > 4 or sum(r.kind == "video" for r in self.references) > 1:
                raise ValueError("unsupported_references")
            if len({r.project_id for r in self.references}) != 1:
                raise ValueError("reference_project_mismatch")
        return self

    def arguments(self, operation: str, source_path=None, media_ids=None):
        result = ["generate", operation, provider_model(self.model), "--prompt", self.prompt,
                  "--duration", str(self.duration_seconds), "--resolution", self.resolution,
                  "--aspect_ratio", self.aspect_ratio]
        if self.model == "seedance_2_5":
            result.extend(["--mode", "t2v"])
        elif self.model == "seedance_2_5_draft":
            result.extend(["--mode", "omni_reference", "--draft", "true"])
        if source_path is not None:
            result.extend(["--start-image", str(source_path)])
        if self.references:
            if media_ids is None or len(media_ids) != len(self.references):
                raise ValueError("reference_upload_required")
            for reference, identity in zip(self.references, media_ids):
                result.extend(["--image-references" if reference.kind == "image" else "--video-references", identity])
        return result


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^video_task_[0-9a-f]{32}$")
    binding_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    organization_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    user_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    project_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    generation: Generation
    price_ceiling: float | int | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def reference_scope(self):
        if any(r.project_id != self.project_id for r in self.generation.references):
            raise ValueError("reference_project_mismatch")
        return self
