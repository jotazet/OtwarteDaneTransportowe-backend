from django import forms

from blog.models import Post


class PostAdminForm(forms.ModelForm):
    """Admin form for Post: show current user as a read-only hint on add."""

    current_author = forms.CharField(
        label='Author',
        required=False,
        disabled=True,
        help_text='Author is automatically set to the currently logged-in user.',
    )

    class Meta:
        model = Post
        fields = ['title', 'tags', 'content', 'image']

    def __init__(self, *args, **kwargs):
        request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)

        # Only show the read-only author field on add (when instance is not saved yet)
        if self.instance and getattr(self.instance, 'pk', None):
            self.fields['current_author'].widget = forms.HiddenInput()
        else:
            username = None
            if request is not None and getattr(request, 'user', None) is not None:
                username = getattr(request.user, 'username', None) or str(request.user)
            self.fields['current_author'].initial = username or '-'

