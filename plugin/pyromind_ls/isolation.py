"""Give every account a private organization, and keep it that way.

Label Studio decides which projects an account can see with a single filter
(projects/api.py):

    Project.objects.filter(organization=self.request.user.active_organization)

So isolation means one organization per account. Two stock behaviours prevent
that: registration puts every new account into the first organization, and
`active_organization` is writable, so any account could point itself at another
account's organization and read their projects.
"""

from organizations.functions import create_organization
from organizations.models import Organization, OrganizationMember
from users.functions.common import save_user as _upstream_save_user
from users.serializers import BaseUserSerializerUpdate


def organization_for(user):
    """The account's own organization, or None if it does not have one yet."""
    return Organization.objects.filter(created_by=user).first()


def ensure_private_organization(user):
    """Move an account into an organization of its own.

    The first account to register owns the organization upstream created for
    it. `created_by` is unique, so it cannot be given a second one.
    """
    existing = organization_for(user)
    if existing is not None:
        return existing

    organization = create_organization(created_by=user, title=user.email)
    user.active_organization = organization
    user.save(update_fields=['active_organization'])
    # A leftover membership in the shared organization would still expose every
    # other account's email through that organization's member list.
    OrganizationMember.objects.filter(user=user).exclude(
        organization=organization
    ).delete()
    return organization


def save_user(request, next_page, user_form):
    """Register the account, then move it out of the shared organization.

    Upstream runs first so that account creation, the post-registration redirect
    and the login stay on Label Studio's own code path.
    """
    response = _upstream_save_user(request, next_page, user_form)
    ensure_private_organization(request.user)
    return response


class UserSerializerUpdate(BaseUserSerializerUpdate):
    """Keeps `active_organization` out of reach of the account API.

    It is the only input to the visibility filter above, so leaving it writable
    lets any authenticated account switch to another account's organization.
    Label Studio answers 405 when a PATCH touches a read-only field, which
    rejects the attempt explicitly instead of silently ignoring it.
    """

    class Meta(BaseUserSerializerUpdate.Meta):
        read_only_fields = BaseUserSerializerUpdate.Meta.read_only_fields + (
            'active_organization',
        )
