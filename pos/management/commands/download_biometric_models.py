import os
import urllib.request
from django.conf import settings
from django.core.management.base import BaseCommand

YUNET_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
SFACE_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx'


class Command(BaseCommand):
    help = 'Descarga los modelos ONNX de detección (YuNet) y reconocimiento facial (SFace).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Fuerza la descarga incluso si los archivos ya existen.',
        )

    def handle(self, *args, **options):
        models_dir = os.path.join(settings.BASE_DIR, 'models')
        os.makedirs(models_dir, exist_ok=True)

        yunet_path = os.path.join(models_dir, 'face_detection_yunet_2023mar.onnx')
        sface_path = os.path.join(models_dir, 'face_recognition_sface_2021dec.onnx')
        force = options.get('force', False)

        for name, url, path in [
            ('YuNet (detección facial)', YUNET_URL, yunet_path),
            ('SFace (reconocimiento facial)', SFACE_URL, sface_path),
        ]:
            if not os.path.exists(path) or force:
                self.stdout.write(f"Descargando {name} desde {url}...")
                urllib.request.urlretrieve(url, path)
                self.stdout.write(self.style.SUCCESS(f"Guardado en {path}"))
            else:
                self.stdout.write(f"{name} ya existe en {path}. Omitiendo.")

        self.stdout.write(self.style.SUCCESS("Modelos biométricos listos para usar."))
