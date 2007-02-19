from zope.component import getUtilitiesFor, queryUtility

from Products.Five.browser import BrowserView
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile

from Acquisition import aq_inner, aq_parent, aq_base
from AccessControl import Unauthorized

from Products.CMFCore.utils import getToolByName
from Products.CMFCore import permissions

from plone.memoize.instance import memoize, clearafter

from plone.app.workflow.interfaces import ISharingPageRole

class PrefsTypesView(BrowserView):
    
    # Actions
    
    template = ViewPageTemplateFile('prefs_types.pt')
    
    def __call__(self):
        """Perform the update and redirect if necessary, or render the page
        """
        
        postback = True
        
        form = self.request.form
        submitted = form.get('form.submitted', False)
    
        save_button = form.get('form.button.Save', None) is not None
        cancel_button = form.get('form.button.Cancel', None) is not None
    
        if submitted and not cancel_button:
            
            # Update the acquire-roles setting
            inherit = bool(form.get('inherit', False))
            self.update_inherit(inherit)

            # Update settings for users and groups
            entries = form.get('entries', [])
            roles = [r['id'] for r in self.roles()]
            settings = []
            for entry in entries:
                settings.append(
                    dict(id = entry['id'],
                         type = entry['type'],
                         roles = [r for r in roles if entry.get('role_%s' % r, False)]))
            if settings:
                self.update_role_settings(settings)
            
        # Other buttons return to the sharing page
        if save_button or cancel_button:
            postback = False
        
        if postback:
            return self.template()
        else:
            self.request.response.redirect(self.context.absolute_url())
            
    # View

    @memoize
    def current_workflow(self, type_id):
        """Given the portal type return the current workflow
        """
        context = aq_inner(self.context)
        portal_workflow = getToolByName(context, 'portal_workflow')
        return portal_workflow.getChainForPortalType(type_id[0])
    
    @memoize
    def roles(self):
        """Get a list of roles that can be managed.
        
        Returns a list of dics with keys:
        
            - id
            - title
        """
        context = aq_inner(self.context)
        portal_membership = getToolByName(context, 'portal_membership')
        
        pairs = []
        
        for name, utility in getUtilitiesFor(ISharingPageRole):
            permission = utility.required_permission
            if permission is None or portal_membership.checkPermission(permission, context):
                pairs.append(dict(id = name, title = utility.title))
                
        pairs.sort(lambda x, y: cmp(x['id'], y['id']))
        return pairs
        
    @memoize
    def role_settings(self):
        """Get current settings for users and groups for which settings have been made.
        
        Returns a list of dicts with keys:
        
         - id
         - title
         - type (one of 'group' or 'user')
         - roles
         
        'roles' is a dict of settings, with keys of role ids as returned by 
        roles(), and values True if the role is explicitly set, False
        if the role is explicitly disabled and None if the role is inherited.
        """
        
        existing_settings = self.existing_role_settings()
        user_results = self.user_search_results()
        group_results = self.group_search_results()

        return existing_settings + user_results + group_results
        
    def inherited(self, context=None):
        """Return True if local roles are inherited here.
        """
        if context is None:
            context = self.context
        if getattr(aq_base(context), '__ac_local_roles_block__', None):
            return False
        return True
        
    # helper functions
    
    @memoize
    def existing_role_settings(self):
        """Get current settings for users and groups that have already got
        at least one of the managed local roles.

        Returns a list of dicts as per role_settings()
        """
        
        context = aq_inner(self.context)
        
        portal_membership = getToolByName(aq_inner(self.context), 'portal_membership')
        portal_groups = getToolByName(aq_inner(self.context), 'portal_groups')
        portal = getToolByName(aq_inner(self.context), 'portal_url').getPortalObject()
        acl_users = getattr(portal, 'acl_users')
        
        info = []
        
        # This logic is adapted from computeRoleMap.py
        
        local_roles = acl_users.getLocalRolesForDisplay(context)
        acquired_roles = self._inherited_roles()
        available_roles = [r['id'] for r in self.roles()]

        # first process acquired roles
        items = {}
        for name, roles, rtype, rid in acquired_roles:
            items[rid] = dict(id       = rid,
                              name     = name,
                              type     = rtype,
                              acquired = roles,
                              local    = [],)
                                
        # second process local roles
        for name, roles, rtype, rid in local_roles:
            if items.has_key(rid):
                items[rid]['local'] = roles
            else:
                items[rid] = dict(id       = rid,
                                  name     = name,
                                  type     = rtype,
                                  acquired = [],
                                  local    = roles,)

        # Sort the list: first Owner role, then groups, then users, and then alphabetically

        dec_users = [('Owner' not in a['local'], a['type'], a['name'], a) for a in items.values()]
        dec_users.sort()
        
        # Add the items to the info dict, assigning full name if possible.
        # Also, recut roles in the format specified in the docstring
        
        for d in dec_users:
            item = d[-1]
            name = item['name']
            rid = item['id']
            
            if item['type'] == 'user':
                member = portal_membership.getMemberInfo(rid)
                if member is not None:
                    name = member['fullname'] or member['username'] or name
            elif item['type'] == 'group':
                g = portal_groups.getGroupById(rid)
                name = g.getGroupTitleOrName()
            
            info_item = dict(id    = item['id'],
                             type  = item['type'],
                             title = name,
                             roles = {})
                             
            # Record role settings
            have_roles = False
            for r in available_roles:
                if r in item['acquired']:
                    info_item['roles'][r] = None
                    have_roles = True # we want to show acquired roles
                elif r in item['local']:
                    info_item['roles'][r] = True
                    have_roles = True # at least one role is set
                else:
                    info_item['roles'][r] = False
                    
            # Abort if we have no useful roles to manage
            if not have_roles:
                continue
                             
            # Use full name if possible
            if not item['type'] == 'group':
                member = portal_membership.getMemberInfo(name)
                if member is not None and member['fullname']:
                    info_item['name'] = member['fullname']
                    
            info.append(info_item)
            
        return info
        
    def user_search_results(self):
        """Return search results for a query to add new users
        
        Returns a list of dicts, as per role_settings()
        """
        search_term = self.request.form.get('search_term', None)
        if not search_term:
            return []
            
        existing_users = set([u['id'] for u in self.existing_role_settings() 
                                if u['type'] == 'user'])
        empty_roles = dict([(r['id'], False) for r in self.roles()])
        info = []
        
        portal_membership = getToolByName(aq_inner(self.context), 'portal_membership')
        for m in portal_membership.searchForMembers(name=search_term):
            user_id = m.getId()
            if user_id not in existing_users:
                info.append(dict(id    = user_id,
                                 title = m.getProperty('fullname', None) or m.getUserName(),
                                 type  = 'user',
                                 roles = empty_roles.copy()))
        return info
        
    def group_search_results(self):
        """Return search results for a query to add new groups
        
        Returns a list of dicts, as per role_settings()
        """
        
        search_term = self.request.form.get('search_term', None)
        if not search_term:
            return []
            
        existing_groups = set([g['id'] for g in self.existing_role_settings() 
                                if g['type'] == 'group'])
        empty_roles = dict([(r['id'], False) for r in self.roles()])
        info = []
        
        portal_groups = getToolByName(aq_inner(self.context), 'portal_groups')
        for g in portal_groups.searchForGroups(name=search_term):
            group_id = g.getId()
            if group_id not in existing_groups:
                info.append(dict(id    = group_id,
                                 title = g.getGroupTitleOrName(),
                                 type  = 'group',
                                 roles = empty_roles.copy()))
        return info
        
    def _inherited_roles(self):
        """Returns a tuple with the acquired local roles."""
        context = aq_inner(self.context)
        
        if not self.inherited(context):
            return []
        
        portal = getToolByName(context, 'portal_url').getPortalObject()
        result = []
        cont = True
        if portal != context:
            parent = aq_parent(context)
            while cont:
                if not getattr(parent, 'acl_users', False):
                    break
                userroles = parent.acl_users.getLocalRolesForDisplay(parent)
                for user, roles, role_type, name in userroles:
                    # Find user in result
                    found = 0
                    for user2, roles2, type2, name2 in result:
                        if user2 == user:
                            # Check which roles must be added to roles2
                            for role in roles:
                                if not role in roles2:
                                    roles2.append(role)
                            found = 1
                            break
                    if found == 0:
                        # Add it to result and make sure roles is a list so
                        # we may append and not overwrite the loop variable
                        result.append([user, list(roles), role_type, name])
                if parent == portal:
                    cont = False
                elif not self.inherited(parent):
                    # Role acquired check here
                    cont = False
                else:
                    parent = aq_parent(parent)

        # Tuplize all inner roles
        for pos in range(len(result)-1,-1,-1):
            result[pos][1] = tuple(result[pos][1])
            result[pos] = tuple(result[pos])

        return tuple(result)
        
    def update_inherit(self, status=True):
        """Enable or disable local role acquisition on the context.
        """
        context = aq_inner(self.context)
        portal_membership = getToolByName(context, 'portal_membership')
        
        if not portal_membership.checkPermission(permissions.ModifyPortalContent, context):
            raise Unauthorized

        if not status:
            context.__ac_local_roles_block__ = True
        else:
            if getattr(aq_base(context), '__ac_local_roles_block__', None):
                context.__ac_local_roles_block__ = None

        context.reindexObjectSecurity()
        
    @clearafter
    def update_role_settings(self, new_settings):
        """Update local role settings and reindex object security if necessary.
        
        new_settings is a list of dicts with keys id, for the user/group id;
        type, being either 'user' or 'group'; and roles, containing the list
        of role ids that are set.
        """
        
        reindex = False
        context = aq_inner(self.context)
            
        managed_roles = frozenset([r['id'] for r in self.roles()])
        member_ids_to_clear = []
            
        for s in new_settings:
            user_id = s['id']
            
            existing_roles = frozenset(context.get_local_roles_for_userid(userid=user_id))
            selected_roles = frozenset(s['roles'])
            
            # We will remove those roles that we are managing and which set
            # on the context, but which were not selected
            to_remove = (managed_roles & existing_roles) - selected_roles
            
            # Leaving us with the selected roles, less any roles that we
            # want to remove
            new_roles = (selected_roles | existing_roles) - to_remove
            
            # take away roles that we are managing, that were not selected 
            # and which were part of the existing roles
            
            if new_roles:
                context.manage_setLocalRoles(user_id, list(new_roles))
                reindex = True
            elif existing_roles:
                member_ids_to_clear.append(user_id)
                
        if member_ids_to_clear:
            context.manage_delLocalRoles(userids=member_ids_to_clear)
            reindex = True
        
        if reindex:
            self.context.reindexObjectSecurity()
