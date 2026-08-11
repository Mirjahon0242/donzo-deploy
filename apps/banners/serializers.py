from rest_framework import serializers
from .models import Banner


class BannerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Banner
        fields = ['id', 'type', 'title', 'subtitle', 'image_url', 'link_url', 'is_active', 'start_date', 'end_date', 'created_at']
        read_only_fields = ['id', 'created_at']
