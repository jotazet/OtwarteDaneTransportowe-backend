from django.contrib import admin

from blog.forms import PostAdminForm
from blog.models import Post, Reaction


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'author', 'content_preview', 'date', 'updated_at')
    list_filter = ('date', 'updated_at', 'author')
    search_fields = ('title', 'content', 'author__username', 'author__email')

    def content_preview(self, obj):
        """Short preview of the content field (first 150 chars)."""
        if not getattr(obj, 'content', None):
            return ''
        text = obj.content
        if len(text) > 150:
            return text[:150] + '…'
        return text

    content_preview.short_description = 'Content (preview)'

    def get_readonly_fields(self, request, obj=None):
        # On edit: keep author visible but not editable.
        if obj is not None:
            return ('author',)
        return ()

    def get_exclude(self, request, obj=None):
        # Always hide the real FK field in the UI; we show a read-only display on add.
        return ('author',)

    def get_form(self, request, obj=None, **kwargs):
        # Inject request into the form so it can render current username.
        kwargs['form'] = PostAdminForm
        form_class = super().get_form(request, obj, **kwargs)

        class RequestBoundForm(form_class):
            def __new__(cls, *args, **kws):
                kws['request'] = request
                return form_class(*args, **kws)

        return RequestBoundForm

    def save_model(self, request, obj, form, change):
        # Always enforce: author is the currently logged-in user.
        if not getattr(obj, 'author_id', None):
            obj.author = request.user
        super().save_model(request, obj, form, change)


@admin.register(Reaction)
class ReactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'post', 'ip_address', 'reaction', 'date')
    list_filter = ('reaction', 'date')
    search_fields = ('ip_address',)
