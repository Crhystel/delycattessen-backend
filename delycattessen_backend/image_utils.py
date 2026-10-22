import io
import os
from PIL import Image
from django.core.files.base import ContentFile


def resize_image_field(field_file, max_dimension: int = 1024, quality: int = 85) -> bool:
    """
    Resizes an image FieldFile in-place using Pillow to a maximum dimension of
    `max_dimension` pixels per side (preserving aspect ratio) and encodes it as JPEG
    with the given quality (default 85).
    Does not upscale images smaller than `max_dimension`.
    Returns True if the image was processed/modified, False otherwise.
    """
    if not field_file or not getattr(field_file, 'name', None):
        return False

    try:
        field_file.seek(0)
        with Image.open(field_file) as img:
            orig_w, orig_h = img.size
            needs_downscale = orig_w > max_dimension or orig_h > max_dimension
            is_jpeg = getattr(img, 'format', '').upper() in ('JPEG', 'JPG')

            # If already smaller/equal than max_dimension and already JPEG, no re-compression needed
            if not needs_downscale and is_jpeg:
                field_file.seek(0)
                return False

            # Convert color mode to RGB if transparent or indexed
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            if needs_downscale:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            buffer.seek(0)

            orig_name = field_file.name
            base_name, _ = os.path.splitext(os.path.basename(orig_name))
            new_name = f"{base_name}.jpg"

            field_file.save(new_name, ContentFile(buffer.getvalue()), save=False)
            return True
    except Exception:
        try:
            field_file.seek(0)
        except Exception:
            pass
        return False
