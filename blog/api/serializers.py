from django.db.models import Count
from rest_framework import serializers

from blog.models import Post, Reaction


class PostSerializer(serializers.ModelSerializer):
    author_username = serializers.CharField(source='author.username', read_only=True)
    reactions_summary = serializers.SerializerMethodField()
    your_reaction = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            'id',
            'title',
            'author',
            'author_username',
            'tags',
            'content',
            'image',
            'date',
            'updated_at',
            'reactions_summary',
            'your_reaction',
        ]
        read_only_fields = ['author', 'author_username', 'date', 'updated_at', 'reactions_summary', 'your_reaction']

    def get_reactions_summary(self, obj: Post):
        # Always return all possible reaction keys, even if count is 0
        base = {key: 0 for key, _ in Reaction.REACTION_CHOICES}

        # If Reaction objects were prefetched, use them (avoids N+1).
        cache = getattr(obj, '_prefetched_objects_cache', {}) or {}
        prefetched = cache.get('reaction_set')
        if prefetched is not None:
            for r in prefetched:
                # Ignore soft-removed reactions
                if not r.reaction:
                    continue
                base[r.reaction] = base.get(r.reaction, 0) + 1
            return base

        rows = (
            Reaction.objects.filter(post=obj, reaction__isnull=False)
            .values('reaction')
            .annotate(count=Count('id'))
        )
        for row in rows:
            base[row['reaction']] = row['count']
        return base

    def get_your_reaction(self, obj: Post):
        """Return the reaction from the current user's IP, or null if none."""
        request = self.context.get('request')
        if not request:
            return None

        # Get client IP using the same logic as in ReactionViewSet
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            client_ip = x_forwarded_for.split(',')[0].strip()
        else:
            client_ip = request.META.get('REMOTE_ADDR')

        if not client_ip:
            return None

        # Check if there's a reaction from this IP for this post
        try:
            reaction = Reaction.objects.get(post=obj, ip_address=client_ip)
            return reaction.reaction or None
        except Reaction.DoesNotExist:
            return None


class PostListSerializer(PostSerializer):
    # Return a truncated preview of content (max 400 characters).
    content = serializers.SerializerMethodField()

    def get_content(self, obj: Post) -> str:
        text = obj.content or ''
        if len(text) <= 400:
            return text
        return text[:400] + '...'


class ReactionSerializer(serializers.ModelSerializer):
    # ip_address is read-only: it will always be set from request.META in the view
    ip_address = serializers.IPAddressField(read_only=True)
    post = serializers.PrimaryKeyRelatedField(read_only=True)
    # Allow sending empty/nullable reaction to indicate "remove/hide" action.
    # Use ChoiceField to show available options in API
    reaction = serializers.ChoiceField(
        choices=Reaction.REACTION_CHOICES,
        allow_null=True,
        allow_blank=True,
        required=False
    )

    class Meta:
        model = Reaction
        fields = ['id', 'post', 'ip_address', 'reaction', 'date']
        read_only_fields = ['post', 'ip_address', 'date']
