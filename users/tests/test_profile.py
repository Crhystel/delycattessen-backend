from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users.models import CustomUser, Institution, ParentProfile, StudentProfile

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
PROFILE = '/api/users/me/profile/'


class ProfileEditionTests(APITestCase):
    """The parent edits their profile and their children's; the teacher only theirs."""

    def setUp(self):
        make = CustomUser.objects.create_user
        institution = Institution.objects.create(name='Sede A')
        self.teacher = make(username='teacher', email='t@x.com', password='x',
                            role=CustomUser.Role.TEACHER, institution=institution)
        self.staff = make(username='staff', email='staff@x.com', password='x',
                          role=CustomUser.Role.OPERATIONS_STAFF, institution=institution)
        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=self.parent_user)
        self.other_parent_user = make(username='other', email='o@x.com', password='x', role=CustomUser.Role.PARENT)
        ParentProfile.objects.create(user=self.other_parent_user)
        self.student_user = make(username='student', email=None, password='x', role=CustomUser.Role.STUDENT,
                                 first_name='ANA', last_name='PEREZ')
        self.student = StudentProfile.objects.create(
            user=self.student_user, parent=parent, institution=institution,
            profile_picture=SimpleUploadedFile('s.gif', GIF, content_type='image/gif'))

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _child_url(self, student=None):
        return f'/api/users/children/{(student or self.student).id}/'

    # ---- own profile -------------------------------------------------------
    def test_parent_sets_names_and_email(self):
        self._auth(self.parent_user)
        response = self.client.patch(PROFILE, {
            'first_name': 'maría', 'second_name': 'josé', 'last_name': 'López',
            'second_last_name': '', 'email': 'Nuevo@X.com',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            (response.data['first_name'], response.data['second_name'], response.data['last_name']),
            ('MARÍA', 'JOSÉ', 'LÓPEZ'),
        )
        self.assertEqual(response.data['email'], 'nuevo@x.com')
        self.assertEqual(response.data['role'], CustomUser.Role.PARENT)
        self.parent_user.refresh_from_db()
        self.assertEqual(self.parent_user.email, 'nuevo@x.com')

    def test_teacher_edits_only_their_profile(self):
        self._auth(self.teacher)
        response = self.client.patch(PROFILE, {'first_name': 'Luis', 'last_name': 'Mora'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.teacher.refresh_from_db()
        self.assertEqual((self.teacher.first_name, self.teacher.last_name), ('LUIS', 'MORA'))
        self.assertEqual(self.client.patch(self._child_url(), {'first_name': 'X'}, format='json').status_code, 403)

    def test_email_must_be_unique(self):
        self._auth(self.parent_user)
        response = self.client.patch(PROFILE, {'email': 'T@x.com'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.patch(PROFILE, {'email': 'p@x.com'}, format='json').status_code, 200)

    def test_required_names_cannot_be_blank(self):
        self._auth(self.parent_user)
        for field in ('first_name', 'last_name'):
            with self.subTest(field=field):
                self.assertEqual(self.client.patch(PROFILE, {field: ''}, format='json').status_code, 400)

    def test_other_roles_cannot_edit_their_profile(self):
        for user in (self.staff, self.student_user):
            self._auth(user)
            self.assertEqual(self.client.patch(PROFILE, {'first_name': 'X'}, format='json').status_code, 403)

    def test_me_returns_the_four_name_parts(self):
        self._auth(self.parent_user)
        self.client.patch(PROFILE, {'first_name': 'Ana', 'second_last_name': 'Ruiz'}, format='json')
        data = self.client.get('/api/users/me/').data
        self.assertEqual((data['first_name'], data['second_last_name']), ('ANA', 'RUIZ'))

    # ---- children ----------------------------------------------------------
    def test_parent_edits_child_names_and_username(self):
        self._auth(self.parent_user)
        response = self.client.patch(self._child_url(), {
            'first_name': 'ana', 'second_name': 'lucía', 'last_name': 'pérez',
            'second_last_name': 'gómez', 'username': 'ana.perez',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            (response.data['first_name'], response.data['second_name'], response.data['second_last_name']),
            ('ANA', 'LUCÍA', 'GÓMEZ'),
        )
        self.assertEqual(response.data['username'], 'ana.perez')
        self.assertIn('balance', response.data)

    def test_child_username_must_be_unique(self):
        self._auth(self.parent_user)
        self.assertEqual(self.client.patch(self._child_url(), {'username': 'teacher'}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(self._child_url(), {'username': 'student'}, format='json').status_code, 200)

    def test_parent_cannot_edit_another_family_child(self):
        self._auth(self.other_parent_user)
        self.assertEqual(self.client.patch(self._child_url(), {'first_name': 'X'}, format='json').status_code, 403)
        self.student_user.refresh_from_db()
        self.assertEqual(self.student_user.first_name, 'ANA')
