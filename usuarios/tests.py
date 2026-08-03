from rest_framework.test import APITestCase
from .models import CustomUser, Sede


class AislamientoSedeTests(APITestCase):
    def setUp(self):
        self.sede_a = Sede.objects.create(nombre="Martim Cereré")
        self.sede_b = Sede.objects.create(nombre="El Sauce")
        self.admin_a = CustomUser.objects.create_user(
            username="admin_a", email="admin_a@delycattessen.com",
            password="test1234", rol=CustomUser.Role.ADMIN, sede=self.sede_a
        )
        self.operativo_b = CustomUser.objects.create_user(
            username="operativo_b", email="operativo_b@delycattessen.com",
            password="test1234", rol=CustomUser.Role.PERSONAL_OPERATIVO, sede=self.sede_b
        )

    def test_usuarios_pertenecen_a_su_sede_correspondiente(self):
        self.assertEqual(self.admin_a.sede, self.sede_a)
        self.assertNotEqual(self.admin_a.sede, self.operativo_b.sede)

    def test_administrador_no_ve_personal_de_otra_sede(self):
        from .mixins import AislamientoPorSedeMixin

        class VistaFalsa:
            def __init__(self, request):
                self.request = request
            def get_queryset(self):
                return CustomUser.objects.filter(rol=CustomUser.Role.PERSONAL_OPERATIVO)

        class VistaConMixin(AislamientoPorSedeMixin, VistaFalsa):
            pass

        class RequestFalso:
            user = self.admin_a

        vista = VistaConMixin(RequestFalso())
        resultado = vista.get_queryset()
        self.assertNotIn(self.operativo_b, resultado)