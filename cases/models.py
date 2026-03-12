from django.db import models, transaction

class CaseStatus(models.Model):
    STATUS_CHOICES = [
        ('none', 'None'),
        ('requested', 'Data Requested'),
        ('denial', 'Data Denial'),
        ('court_referral', 'Court Referral'),
        ('ministry_complaint', 'Complaint to the Ministry'),
        ('not_available', 'Data not available'),
        ('received', 'Data Received'),
        ('no_contract', 'No data contract with provider'),
        ('reminder', 'Reminder message'),
        ('phone_call', 'Phone call'),
        ('other', 'Other'),
    ]

    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='none')
    date = models.DateTimeField(auto_now_add=True)
    description = models.TextField(blank=True, null=True)
    case = models.ForeignKey('TransportOrganization', related_name='case_status', on_delete=models.CASCADE)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['case', '-date']),
        ]

    def __str__(self):
        return f"{self.get_status_display()} ({self.date:%Y-%m-%d})"


class DataProvider(models.Model):
    name = models.CharField(max_length=100, unique=True)
    website = models.URLField(blank=True, null=True)
    contact_email = models.EmailField(blank=True, null=True)

    def __str__(self):
        return self.name


class TransportOrganization(models.Model):
    data_providers = models.ManyToManyField(DataProvider, related_name="transport_organizations", blank=True)
    region = models.CharField(max_length=50)
    transport_organization = models.CharField(max_length=100)
    website = models.URLField(blank=True, null=True)
    contact_email = models.EmailField(blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    is_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['region', 'transport_organization']
        constraints = [
            models.UniqueConstraint(
                fields=['region', 'transport_organization'],
                name='unique_public_transport_region_org',
            )
        ]
        indexes = [
            models.Index(fields=['region', 'transport_organization']),
        ]

    def __str__(self):
        return f"{self.region} ({self.transport_organization})"

    @transaction.atomic
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            CaseStatus.objects.create(case=self, status='none')