from django.db import models


class Banner(models.Model):
    BANNER_TYPES = [
        ('desktop', 'Desktop'),
        ('mobile', 'Mobile'),
        ('popup', 'Popup'),
        ('slider', 'Slider'),
        ('announcement', "E'lon"),
    ]

    type = models.CharField(max_length=20, choices=BANNER_TYPES)
    title = models.CharField(max_length=200, blank=True, default='')
    subtitle = models.CharField(max_length=300, blank=True, default='')
    image_url = models.URLField()
    link_url = models.URLField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'banners'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_type_display()} Banner"
