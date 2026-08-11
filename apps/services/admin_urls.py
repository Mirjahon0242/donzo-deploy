from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'services', views.AdminServiceViewSet)
router.register(r'packages', views.AdminPackageViewSet)
router.register(r'fields', views.AdminServiceFieldViewSet)
router.register(r'categories', views.AdminCategoryViewSet)

urlpatterns = router.urls
