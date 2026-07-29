from django.db.models import Count
from rest_framework import serializers

from OtwarteDaneTransportowe.request_ip import get_client_ip
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
        """Return the reaction from the current user's IP, or null if none.

        Only meaningful for requests made DIRECTLY by the visitor's browser.
        A server-side renderer (Next.js SSR) calls the API from its own host,
        so this field would then describe the frontend server, identically for
        every visitor. Such clients must read their own state from
        ``GET /api/blog/reactions/mine/`` in the browser instead.
        """
        request = self.context.get('request')
        if not request:
            return None

        # Same trusted-proxy-aware IP resolution as ReactionViewSet, so reads
        # and writes agree and a forged X-Forwarded-For cannot select another
        # client's reaction.
        client_ip = get_client_ip(request)
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
