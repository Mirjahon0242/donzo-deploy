from rest_framework.permissions import BasePermission
from .models import Role


class IsAdmin(BasePermission):
    """Allow access only to Admin or Super Admin."""
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in [Role.ADMIN, Role.SUPER_ADMIN]


class IsSuperAdmin(BasePermission):
    """Allow access only to Super Admin."""
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == Role.SUPER_ADMIN


class IsOperator(BasePermission):
    """Allow access to Operators and above."""
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in [
            Role.OPERATOR, Role.SENIOR_OPERATOR, Role.ADMIN, Role.SUPER_ADMIN
        ]


class IsOwnerOrAdmin(BasePermission):
    """Object-level permission: only owner or admin can access."""
    def has_object_permission(self, request, view, obj):
        if request.user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
            return True
        return hasattr(obj, 'customer') and obj.customer == request.user


class IsAssignedOperator(BasePermission):
    """Check if the operator is assigned to this service."""
    def has_object_permission(self, request, view, obj):
        if request.user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
            return True
        return (
            hasattr(obj, 'service') and
            obj.service.allowed_operators.filter(id=request.user.id).exists()
        )
