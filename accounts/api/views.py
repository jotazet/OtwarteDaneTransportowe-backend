from django.contrib.auth import get_user_model
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from OtwarteDaneTransportowe.auth_roles import RequiresAdminGroup
from accounts.api.serializers import (
    ChangeEmailSerializer,
    ChangePasswordSerializer,
    PasswordResetResponseSerializer,
    UserCreateSerializer,
    UserSerializer,
    UserUpdateSerializer,
)
from accounts.services import (
    generate_random_password,
    would_remove_last_admin,
)

User = get_user_model()


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by('username')
    permission_classes = [IsAuthenticated, RequiresAdminGroup]

    def get_queryset(self):
        return User.objects.prefetch_related('groups').order_by('username')

    def get_permissions(self):
        if self.action in ('me', 'change_email', 'change_password'):
            return [IsAuthenticated()]
        return [IsAuthenticated(), RequiresAdminGroup()]

    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        if self.action in ('update', 'partial_update'):
            return UserUpdateSerializer
        if self.action == 'reset_password':
            return PasswordResetResponseSerializer
        if self.action == 'me':
            return UserSerializer
        return UserSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        response_data = UserSerializer(user).data
        response_data['generated_password'] = serializer.context['generated_password']
        headers = self.get_success_headers(response_data)
        return Response(response_data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            raise ValidationError('You cannot delete your own account.')
        if would_remove_last_admin(instance, new_roles=None):
            raise ValidationError('Cannot delete the last user with the Admin role.')
        instance.delete()

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        instance.refresh_from_db()
        return Response(UserSerializer(instance).data)

    def perform_update(self, serializer):
        instance = serializer.instance
        roles = serializer.validated_data.get('roles')
        if roles is not None and would_remove_last_admin(instance, new_roles=roles):
            raise ValidationError('Cannot remove the Admin role from the last admin user.')
        serializer.save()

    @action(detail=True, methods=['post'], url_path='reset-password')
    def reset_password(self, request, pk=None):
        user = self.get_object()
        password = generate_random_password(user=user)
        user.set_password(password)
        user.save(update_fields=['password'])
        data = {
            'id': user.id,
            'username': user.username,
            'generated_password': password,
        }
        return Response(PasswordResetResponseSerializer(data).data)

    @action(detail=False, methods=['get'], url_path='me')
    def me(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='me/change-email')
    def change_email(self, request):
        serializer = ChangeEmailSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data)

    @action(detail=False, methods=['post'], url_path='me/change-password')
    def change_password(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'detail': 'Password updated.'})
