from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    order_index = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'categories'
        verbose_name_plural = 'Categories'
        ordering = ['order_index', 'name']

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Service(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE,
        related_name='services'
    )
    image_url = models.URLField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    instruction_text = models.TextField(
        blank=True, null=True,
        help_text="Mijozga ko'rinadigan yo'riqnoma"
    )
    is_active = models.BooleanField(default=True)
    allowed_operators = models.ManyToManyField(
        'users.User', blank=True,
        related_name='allowed_services',
        limit_choices_to={'role__in': ['operator', 'senior_operator', 'admin', 'super_admin']}
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'services'
        ordering = ['name']

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    @property
    def category_name(self):
        return self.category.name if self.category else ''


class ServiceField(models.Model):
    FIELD_TYPES = [
        ('text', 'Matn'),
        ('number', 'Raqam'),
        ('select', 'Tanlov'),
    ]

    service = models.ForeignKey(
        Service, on_delete=models.CASCADE,
        related_name='fields'
    )
    field_name = models.CharField(max_length=100, help_text="Masalan: game_id, server_id")
    field_label = models.CharField(max_length=200, help_text="Masalan: Game ID")
    field_type = models.CharField(max_length=20, choices=FIELD_TYPES, default='text')
    is_required = models.BooleanField(default=True)
    validation_regex = models.CharField(max_length=500, blank=True, null=True)
    order_index = models.IntegerField(default=0)
    options = models.JSONField(
        default=list, blank=True,
        help_text="Select turi uchun variantlar (masalan: [\"1 oy\", \"3 oy\", \"6 oy\"])"
    )

    class Meta:
        db_table = 'service_fields'
        ordering = ['order_index']

    def __str__(self):
        return f"{self.service.name} - {self.field_label}"


class Package(models.Model):
    service = models.ForeignKey(
        Service, on_delete=models.CASCADE,
        related_name='packages'
    )
    name = models.CharField(max_length=200, help_text="Masalan: 86 Diamond")
    amount_label = models.CharField(max_length=200, blank=True, null=True)
    price = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=10, default='UZS')
    is_active = models.BooleanField(default=True)
    order_index = models.IntegerField(default=0)

    class Meta:
        db_table = 'packages'
        ordering = ['order_index']

    def __str__(self):
        return f"{self.service.name} - {self.name} ({self.price} {self.currency})"
