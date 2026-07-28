from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.conf import settings

from blog.validators import validate_image_file_size

def validate_tags(value):
    if len(value) > 5:
        raise ValidationError("There is limit to 5 tags.")
    for tag in value:
        if len(tag) > 16:
            raise ValidationError(f"Tag '{tag}' is too long (max 16 characters).")

class Post(models.Model):
    title = models.CharField(max_length=24)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='posts')
    tags = ArrayField(models.CharField(max_length=16), size=5, validators=[validate_tags], blank=True, default=list)
    content = models.TextField()
    image = models.ImageField(
        upload_to='blog/images/', blank=True, null=True,
        validators=[validate_image_file_size],
    )
    date = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['date']),
        ]

    def __str__(self):
        return f"Post(id={self.id}, title='{self.title}')"

    # Image files are reclaimed by the post_delete/pre_save receivers in
    # OtwarteDaneTransportowe.cleanup_files (registered via
    # DataManagerConfig.ready()), which also cover bulk and cascade deletes —
    # no delete() override needed.

class Reaction(models.Model):
    REACTION_CHOICES = [
        ('like', 'Like'),
        ('dislike', 'Dislike'),
        ('love', 'Love'),
        ('haha', 'Haha'),
        ('wow', 'Wow'),
        ('sad', 'Sad'),
        ('angry', 'Angry')
    ]
    post = models.ForeignKey(Post, on_delete=models.CASCADE)
    ip_address = models.GenericIPAddressField()
    # NULL/blank means: user has no active reaction (soft-removed)
    reaction = models.CharField(max_length=8, choices=REACTION_CHOICES, null=True, blank=True)
    date = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        constraints = [
            models.UniqueConstraint(fields=['post', 'ip_address'], name='unique_reaction_per_post_ip'),
        ]

    def __str__(self):
        return f"Reaction(post_id={self.post_id}, ip={self.ip_address}, reaction={self.reaction})"
