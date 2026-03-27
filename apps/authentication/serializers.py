"""
Cytova — Authentication Serializers
"""
from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
    )

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get('request'),
            username=attrs['email'],
            password=attrs['password'],
        )
        if user is None:
            raise serializers.ValidationError(
                'Invalid credentials.',
                code='authentication_failed',
            )
        if not user.is_active:
            raise serializers.ValidationError(
                'This account has been deactivated.',
                code='authentication_failed',
            )
        attrs['user'] = user
        return attrs


class TokenRefreshSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()

    def validate(self, attrs):
        try:
            attrs['token'] = RefreshToken(attrs['refresh_token'])
        except TokenError as exc:
            raise serializers.ValidationError({'refresh_token': str(exc)})
        return attrs


class LogoutSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError(
                {'password_confirm': 'Passwords do not match.'}
            )
        return attrs
