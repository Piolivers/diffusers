import os
import tempfile
from typing import Callable, List, Optional, Union

import PIL.Image
import PIL.ImageOps
import requests

from .import_utils import BACKENDS_MAPPING, is_opencv_available


def load_image(
    image: Union[str, PIL.Image.Image], convert_method: Optional[Callable[[PIL.Image.Image], PIL.Image.Image]] = None
) -> PIL.Image.Image:
    """
    Loads `image` to a PIL Image.

    Args:
        image (`str` or `PIL.Image.Image`):
            The image to convert to the PIL Image format.
        convert_method (Callable[[PIL.Image.Image], PIL.Image.Image], *optional*):
            A conversion method to apply to the image after loading it. When set to `None` the image will be converted
            "RGB".

    Returns:
        `PIL.Image.Image`:
            A PIL Image.
    """
    if isinstance(image, str):
        if image.startswith("http://") or image.startswith("https://"):
            image = PIL.Image.open(requests.get(image, stream=True).raw)
        elif os.path.isfile(image):
            image = PIL.Image.open(image)
        else:
            raise ValueError(
                f"Incorrect path or URL. URLs must start with `http://` or `https://`, and {image} is not a valid path."
            )
    elif isinstance(image, PIL.Image.Image):
        image = image
    else:
        raise ValueError(
            "Incorrect format used for the image. Should be a URL linking to an image, a local path, or a PIL image."
        )

    image = PIL.ImageOps.exif_transpose(image)

    if convert_method is not None:
        image = convert_method(image)
    else:
        image = image.convert("RGB")

    return image


def load_video(
    video: Union[str, List[PIL.Image.Image]],
    convert_method: Optional[Callable[[List[PIL.Image.Image]], List[PIL.Image.Image]]] = None,
) -> List[PIL.Image.Image]:
    """
    Loads `video` to a list of PIL Image.

    Args:
        video (`str` or `List[PIL.Image.Image]`):
            The video to convert to a list of PIL Image format.
        convert_method (Callable[[List[PIL.Image.Image]], List[PIL.Image.Image]], *optional*):
            A conversion method to apply to the video after loading it. When set to `None` the images will be converted
            to "RGB".

    Returns:
        `List[PIL.Image.Image]`:
            The video as a list of PIL images.
    """
    if isinstance(video, str):
        was_tempfile_created = False

        if video.startswith("http://") or video.startswith("https://"):
            video_data = requests.get(video, stream=True).raw
            video_path = tempfile.NamedTemporaryFile(suffix=os.path.splitext(video)[1], delete=False).name
            was_tempfile_created = True
            with open(video_path, "wb") as f:
                f.write(video_data.read())
            video = video_path
        elif not os.path.isfile(video):
            raise ValueError(
                f"Incorrect path or URL. URLs must start with `http://` or `https://`, and {video} is not a valid path."
            )

        if video.endswith(".gif"):
            pil_images = []
            gif = PIL.Image.open(video)
            try:
                while True:
                    pil_images.append(gif.copy())
                    gif.seek(gif.tell() + 1)
            except EOFError:
                pass
        else:
            if is_opencv_available():
                import cv2
            else:
                raise ImportError(BACKENDS_MAPPING["opencv"][1].format("load_video"))
            pil_images = []
            video_capture = cv2.VideoCapture(video)
            success, frame = video_capture.read()
            while success:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_images.append(PIL.Image.fromarray(frame))
                success, frame = video_capture.read()
            video_capture.release()

        if was_tempfile_created:
            os.remove(video_path)

    elif isinstance(video, list) and all(isinstance(frame, PIL.Image.Image) for frame in video):
        pil_images = video
    else:
        raise ValueError(
            "Incorrect format used for the video. Should be a URL, a local path, or a list of PIL images."
        )

    if convert_method is not None:
        pil_images = convert_method(pil_images)
    else:
        pil_images = [image.convert("RGB") for image in pil_images]

    return pil_images
