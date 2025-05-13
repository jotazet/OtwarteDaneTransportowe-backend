from django.db import models, transaction

class CaseStatus(models.Model):
    STATUS_CHOICES = [
        ('0', 'None'),
        ('1', 'Data Requested'),
        ('2', 'Data Denial'),
        ('3', 'Court Referral'),
        ('4', 'Complaint to the Ministry'),
        ('5', 'Data not available'),
        ('6', 'Data Received'),
        ('7', 'No data contract with provider'),
        ('8', 'Other'),
    ]

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='0')
    date = models.DateTimeField(auto_now_add=True)
    description = models.TextField(max_length=100, blank=True, null=True)
    case = models.ForeignKey('PublicTransport', related_name='case_status', on_delete=models.CASCADE)
    
    def __str__(self):
        return self.status

class DataProvider(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    website = models.URLField(blank=True, null=True)
    contact_email = models.EmailField(blank=True, null=True)

    def __str__(self):
        return self.name

class PublicTransport(models.Model):
    data_providers = models.ManyToManyField(DataProvider, related_name="public_transports", blank=True)
    region = models.CharField(max_length=50)
    transport_organization = models.CharField(max_length=100, blank=True, null=True)
    website = models.URLField(blank=True, null=True)
    contact_email = models.EmailField(blank=True, null=True)
    provision = models.TextField(max_length=1000, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.region} ({self.transport_organization or 'N/A'})"
        
    @transaction.atomic
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super(PublicTransport, self).save(*args, **kwargs)
        if is_new:
            CaseStatus.objects.create(case=self, status='0') 
            
class DataFeedback(models.Model):
    DATA_TYPE = [
        ('GTFS', 'GTFS'),
        ('GTFS-RT', 'GTFS-RT'),
        ('NeTEx', 'NeTEx'),
        ('SIRI', 'SIRI'),
        ('Other', 'Other'),
        ('None', 'None'),
    ]

    transport_organization = models.ForeignKey(PublicTransport, related_name='feedback', on_delete=models.CASCADE)
    data_foramt = models.CharField(max_length=50, choices=DATA_TYPE, default='Other')
    file = models.FileField(upload_to='public_transport_data/', blank=True, null=True)
    description = models.TextField(max_length=100, blank=True, null=True)
    url_to_data = models.URLField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.data_foramt
