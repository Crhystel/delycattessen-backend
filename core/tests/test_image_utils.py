import io

from django.test import SimpleTestCase
from PIL import Image

from delycattessen_backend.image_utils import ORIENTATION_TAG, resize_image_field


class FakeFieldFile(io.BytesIO):
    """Just what resize_image_field uses of a Django FieldFile."""

    name = 'profiles/students/foto.jpg'
    saved = None

    def save(self, name, content, save=False):
        self.saved = content.read()


def phone_photo(width, height, orientation=6):
    """A JPEG stored sideways (`width` x `height`) with the EXIF tag that tells
    viewers to rotate it, as phone cameras do (6 = rotate 90° clockwise)."""
    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    buffer = FakeFieldFile()
    Image.new('RGB', (width, height), 'white').save(buffer, format='JPEG', exif=exif.tobytes())
    buffer.seek(0)
    return buffer


def saved_size(field_file):
    with Image.open(io.BytesIO(field_file.saved)) as img:
        return img.size


class ResizeImageFieldTests(SimpleTestCase):
    def test_a_large_sideways_photo_is_resized_upright(self):
        photo = phone_photo(2000, 1500)

        self.assertTrue(resize_image_field(photo))

        self.assertEqual(saved_size(photo), (768, 1024))

    def test_a_small_sideways_photo_is_rotated_too(self):
        photo = phone_photo(800, 600)

        self.assertTrue(resize_image_field(photo))

        self.assertEqual(saved_size(photo), (600, 800))

    def test_a_small_upright_jpeg_is_left_alone(self):
        photo = phone_photo(800, 600, orientation=1)

        self.assertFalse(resize_image_field(photo))
        self.assertIsNone(photo.saved)
