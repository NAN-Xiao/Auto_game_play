"""Builder for constructing multimodal chat messages."""

import base64
from io import BytesIO
from typing import Any

from PIL import Image

from AutoGLM_GUI.logger import logger


MODEL_IMAGE_SINGLE_MAX_SIDE = 1024
MODEL_IMAGE_MEDIUM_BATCH_MAX_SIDE = 768
MODEL_IMAGE_LARGE_BATCH_MAX_SIDE = 640
MODEL_IMAGE_SINGLE_JPEG_QUALITY = 72
MODEL_IMAGE_MEDIUM_BATCH_JPEG_QUALITY = 66
MODEL_IMAGE_LARGE_BATCH_JPEG_QUALITY = 60
MODEL_IMAGE_TOTAL_BASE64_BUDGET = 1_200_000
MODEL_IMAGE_MIN_BASE64_BUDGET = 60_000


def _image_budget_for_batch(image_count: int) -> tuple[int, int, int]:
    safe_count = max(1, image_count)
    if safe_count >= 8:
        max_side = MODEL_IMAGE_LARGE_BATCH_MAX_SIDE
        quality = MODEL_IMAGE_LARGE_BATCH_JPEG_QUALITY
    elif safe_count >= 4:
        max_side = MODEL_IMAGE_MEDIUM_BATCH_MAX_SIDE
        quality = MODEL_IMAGE_MEDIUM_BATCH_JPEG_QUALITY
    else:
        max_side = MODEL_IMAGE_SINGLE_MAX_SIDE
        quality = MODEL_IMAGE_SINGLE_JPEG_QUALITY
    per_image_base64_budget = max(
        MODEL_IMAGE_MIN_BASE64_BUDGET,
        MODEL_IMAGE_TOTAL_BASE64_BUDGET // safe_count,
    )
    return max_side, quality, per_image_base64_budget


def _encode_jpeg_base64(
    source: Image.Image,
    *,
    max_side: int,
    quality: int,
) -> str:
    converted = source.convert("RGB")
    converted.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    output = BytesIO()
    converted.save(output, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")


def _prepare_image_for_model(
    image: dict[str, str], *, image_count: int = 1
) -> dict[str, str]:
    """Downscale screenshots before sending them to the model.

    Persisted task events still keep the original screenshot. This only reduces
    the multimodal request payload and model-side image preprocessing latency.
    """

    mime_type = image.get("mime_type", "image/png")
    data = image.get("data", "")
    if not data:
        return image

    try:
        raw = base64.b64decode(data, validate=False)
        max_side, quality, per_image_base64_budget = _image_budget_for_batch(
            image_count
        )
        with Image.open(BytesIO(raw)) as source:
            encoded = _encode_jpeg_base64(
                source,
                max_side=max_side,
                quality=quality,
            )
            while len(encoded) > per_image_base64_budget and max_side > 512:
                max_side = max(512, int(max_side * 0.85))
                quality = max(50, quality - 6)
                encoded = _encode_jpeg_base64(
                    source,
                    max_side=max_side,
                    quality=quality,
                )
        return {
            "mime_type": "image/jpeg",
            "data": encoded,
        }
    except Exception as exc:
        logger.debug("Using original model image payload: {}", exc)
        return {"mime_type": mime_type, "data": data}


class MessageBuilder:
    @staticmethod
    def create_system_message(content: str) -> dict[str, Any]:
        return {"role": "system", "content": content}

    @staticmethod
    def create_user_message(
        text: str, image_base64: str | None = None
    ) -> dict[str, Any]:
        if image_base64 is None:
            return {"role": "user", "content": text}

        return MessageBuilder.create_user_message_with_images(
            text=text,
            images=[{"mime_type": "image/png", "data": image_base64}],
        )

    @staticmethod
    def create_user_message_with_images(
        text: str, images: list[dict[str, str]]
    ) -> dict[str, Any]:
        if not images:
            return {"role": "user", "content": text}

        content_parts: list[dict[str, Any]] = []
        image_count = len(images)
        for image in images:
            model_image = _prepare_image_for_model(image, image_count=image_count)
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{model_image['mime_type']};base64,"
                            f"{model_image['data']}"
                        )
                    },
                }
            )

        # Images first, then text — matches the official Open-AutoGLM input layout.
        content_parts.append({"type": "text", "text": text})
        return {
            "role": "user",
            "content": content_parts,
        }

    @staticmethod
    def create_multi_image_user_message(
        text: str, image_base64_list: list[str]
    ) -> dict[str, Any]:
        if not image_base64_list:
            return {"role": "user", "content": text}

        return MessageBuilder.create_user_message_with_images(
            text=text,
            images=[
                {"mime_type": "image/png", "data": image_base64}
                for image_base64 in image_base64_list
            ],
        )

    @staticmethod
    def build_user_reference_images_notice(image_count: int) -> str:
        if image_count <= 0:
            return ""
        image_word = "image" if image_count == 1 else "images"
        return (
            f"User attached {image_count} reference {image_word}. "
            "Image 1 is the current Android screen and is the only image that "
            "defines tap/swipe coordinates. Use the following attached images "
            "only as reference material for the user's task."
        )

    @staticmethod
    def create_assistant_message(content: str) -> dict[str, Any]:
        return {"role": "assistant", "content": content}

    @staticmethod
    def remove_images_from_message(message: dict[str, Any]) -> dict[str, Any]:
        """Drop image parts from a message, keeping the text parts as a list.

        Mirrors the official Open-AutoGLM behaviour: after a request the
        screenshot is stripped from the user turn so that history never
        carries stale images into later requests. String content (system /
        assistant turns) is returned unchanged.
        """
        content = message.get("content")
        if not isinstance(content, list):
            return message

        text_parts = [
            part
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return {**message, "content": text_parts}

    @staticmethod
    def build_screen_info(current_app: str) -> str:
        return f"** Screen Info **\n\nCurrent App: {current_app}"
