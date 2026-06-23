from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from OtwarteDaneTransportowe.auth_roles import ALLOWED_ROLE_NAMES
from accounts.services import generate_random_password, set_user_roles, user_role_names

User = get_user_model()


def validate_role_names(roles):
    invalid = set(roles) - ALLOWED_ROLE_NAMES
    if invalid:
        raise serializers.ValidationError(
            f'Unknown roles: {sorted(invalid)}. Allowed: {sorted(ALLOWED_ROLE_NAMES)}'
        )
    return roles


class RolesListField(serializers.ListField):
    child = serializers.CharField()

    def to_internal_value(self, data):
        roles = super().to_internal_value(data)
        return validate_role_names(roles)


class UserSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'first_name',
            'last_name',
            'is_active',
            'roles',
        ]
        read_only_fields = fields

    def get_roles(self, obj):
        return user_role_names(obj)


class UserCreateSerializer(serializers.ModelSerializer):
    roles = RolesListField(required=False)

    class Meta:
        model = User
        fields = ['username', 'email', 'first_name', 'last_name', 'roles']

    def validate_roles(self, value):
        return value or []

    def create(self, validated_data):
        roles = validated_data.pop('roles', [])
        password = generate_random_password()
        user = User.objects.create_user(password=password, **validated_data)
        set_user_roles(user, roles)
        self.context['generated_password'] = password
        return user


class UserUpdateSerializer(serializers.ModelSerializer):
    roles = RolesListField(required=False)

    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name', 'is_active', 'roles']

    def update(self, instance, validated_data):
        roles = validated_data.pop('roles', None)
        instance = super().update(instance, validated_data)
        if roles is not None:
            set_user_roles(instance, roles)
        return instance


class PasswordResetResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(read_only=True)
    generated_password = serializers.CharField(read_only=True)


class ChangeEmailSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_email = serializers.EmailField()

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Current password is incorrect.')
        return value

    def save(self, **kwargs):
        user = self.context['request'].user
        user.email = self.validated_data['new_email']
        user.save(update_fields=['email'])
        return user


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Current password is incorrect.')
        return value

    def validate_new_password(self, value):
        user = self.context['request'].user
        try:
            validate_password(value, user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def save(self, **kwargs):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save(update_fields=['password'])
        return user
