from collections import defaultdict
import csv
import datetime
import io
import re
import requests
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm, PasswordChangeForm
from django.contrib.auth.models import Group
from django.core.paginator import EmptyPage, PageNotAnInteger
from django.core.urlresolvers import reverse
from django.core.exceptions import (
    ObjectDoesNotExist,
    PermissionDenied,
)
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.http import HttpResponseBadRequest
from django.db import IntegrityError
from django.db.models import Count, Sum, Q, F, Model, ProtectedError
from django.db.models import Case, When, Value, IntegerField
from django.forms import modelformset_factory
from django.shortcuts import redirect, render, get_object_or_404
from django.template.loader import get_template
from django.views.decorators.http import require_POST
from django.views.generic import ListView, DetailView, View
from django.views.generic.edit import CreateView, UpdateView, ModelFormMixin
from django.contrib.auth.decorators import login_required, permission_required

from reversion import get_for_object
from reversion.models import Revision

from workshops.models import (
    Airport,
    Award,
    Badge,
    Event,
    Qualification,
    Lesson,
    Person,
    Role,
    Host,
    Membership,
    Tag,
    Task,
    EventRequest,
    ProfileUpdateRequest,
    TodoItem,
    TodoItemQuerySet,
)
from workshops.forms import (
    SearchForm, DebriefForm, InstructorsForm, PersonForm, PersonBulkAddForm,
    EventForm, TaskForm, TaskFullForm, bootstrap_helper, bootstrap_helper_get,
    bootstrap_helper_with_add, BadgeAwardForm, PersonAwardForm,
    PersonPermissionsForm, bootstrap_helper_filter, PersonMergeForm,
    PersonTaskForm, HostForm, SWCEventRequestForm, DCEventRequestForm,
    ProfileUpdateRequestForm, PersonLookupForm, bootstrap_helper_wider_labels,
    SimpleTodoForm, bootstrap_helper_inline_formsets, BootstrapHelper,
    AdminLookupForm, ProfileUpdateRequestFormNoCaptcha, MembershipForm,
)
from workshops.util import (
    upload_person_task_csv,  verify_upload_person_task,
    create_uploaded_persons_tasks, InternalError, Paginator, merge_persons,
    update_event_attendance_from_tasks,
    WrongWorkshopURL,
    generate_url_to_event_index,
    find_tags_on_event_index,
    find_tags_on_event_website,
    parse_tags_from_event_website,
    validate_tags_from_event_website,
    assignment_selection,
)

from workshops.filters import (
    EventFilter, HostFilter, PersonFilter, TaskFilter, AirportFilter,
    EventRequestFilter,
)

#------------------------------------------------------------

ITEMS_PER_PAGE = 25

#------------------------------------------------------------


class CreateViewContext(SuccessMessageMixin, CreateView):
    """
    Class-based view for creating objects that extends default template context
    by adding model class used in objects creation.
    """
    success_message = '{name} was created successfully.'

    def get_context_data(self, **kwargs):
        context = super(CreateViewContext, self).get_context_data(**kwargs)

        # self.model is available in CreateView as the model class being
        # used to create new model instance
        context['model'] = self.model

        if self.model and issubclass(self.model, Model):
            context['title'] = 'New {}'.format(self.model._meta.verbose_name)
        else:
            context['title'] = 'New object'

        context['form_helper'] = bootstrap_helper
        return context

    def get_success_message(self, cleaned_data):
        "Format self.success_message, used by messages framework from Django."
        return self.success_message.format(cleaned_data, name=str(self.object))


class UpdateViewContext(SuccessMessageMixin, UpdateView):
    """
    Class-based view for updating objects that extends default template context
    by adding proper page title.
    """
    success_message = '{name} was updated successfully.'

    def get_context_data(self, **kwargs):
        context = super(UpdateViewContext, self).get_context_data(**kwargs)

        # self.model is available in UpdateView as the model class being
        # used to update model instance
        context['model'] = self.model

        context['view'] = self

        # self.object is available in UpdateView as the object being currently
        # edited
        context['title'] = str(self.object)

        context['form_helper'] = bootstrap_helper
        return context

    def get_success_message(self, cleaned_data):
        "Format self.success_message, used by messages framework from Django."
        return self.success_message.format(cleaned_data, name=str(self.object))


class LoginRequiredMixin(object):
    """
    Define @login_required-based mixin for class-based views that should allow
    only logged-in users.

    Based on Django docs:
    https://docs.djangoproject.com/en/1.8/topics/class-based-views/intro/#mixins-that-wrap-as-view
    """

    @classmethod
    def as_view(cls, **kwargs):
        view = super(LoginRequiredMixin, cls).as_view(**kwargs)
        return login_required(view)


class PermissionRequiredMixin(object):
    """
    Mixin for allowing only users with specific permissions to access the view.
    """
    perms = ''  # permission name or a list of them

    @classmethod
    def as_view(cls, **kwargs):
        view = super().as_view(**kwargs)
        return permission_required(cls.perms, raise_exception=True)(view)


#------------------------------------------------------------


@login_required
def dashboard(request):
    '''Home page.'''
    current_events = (
        Event.objects.upcoming_events() | Event.objects.ongoing_events()
    ).no_stalled()
    uninvoiced_events = Event.objects.uninvoiced_events().no_stalled()
    unpublished_events = Event.objects.unpublished_events().no_stalled() \
                                      .select_related('host')

    assigned_to, is_admin = assignment_selection(request)

    if assigned_to == 'me':
        current_events = current_events.filter(assigned_to=request.user)
        uninvoiced_events = uninvoiced_events.filter(assigned_to=request.user)
        unpublished_events = unpublished_events.filter(
            assigned_to=request.user)

    elif assigned_to == 'noone':
        current_events = current_events.filter(assigned_to__isnull=True)
        uninvoiced_events = uninvoiced_events.filter(
            assigned_to__isnull=True)
        unpublished_events = unpublished_events.filter(
            assigned_to__isnull=True)

    elif assigned_to == 'all':
        # no filtering
        pass

    context = {
        'title': None,
        'is_admin': is_admin,
        'assigned_to': assigned_to,
        'current_events': current_events,
        'uninvoiced_events': uninvoiced_events,
        'unpublished_events': unpublished_events,
        'todos_start_date': TodoItemQuerySet.current_week_dates()[0],
        'todos_end_date': TodoItemQuerySet.next_week_dates()[1],
    }
    return render(request, 'workshops/dashboard.html', context)


@login_required
def changes_log(request):
    log = Revision.objects.all().select_related('user') \
                                .prefetch_related('version_set') \
                                .order_by('-date_created')
    log = _get_pagination_items(request, log)
    context = {
        'log': log
    }
    return render(request, 'workshops/changes_log.html', context)

#------------------------------------------------------------


@login_required
def all_hosts(request):
    '''List all hosts.'''

    filter = HostFilter(request.GET, queryset=Host.objects.all())
    hosts = _get_pagination_items(request, filter)
    context = {'title' : 'All Hosts',
               'all_hosts' : hosts,
               'filter': filter,
               'form_helper': bootstrap_helper_filter}
    return render(request, 'workshops/all_hosts.html', context)


@login_required
def host_details(request, host_domain):
    '''List details of a particular host.'''
    host = Host.objects.get(domain=host_domain)
    events = Event.objects.filter(host=host)
    context = {'title' : 'Host {0}'.format(host),
               'host' : host,
               'events' : events}
    return render(request, 'workshops/host.html', context)


class HostCreate(LoginRequiredMixin, PermissionRequiredMixin,
                 CreateViewContext):
    perms = 'workshops.add_host'
    model = Host
    form_class = HostForm
    template_name = 'workshops/generic_form.html'


class HostUpdate(LoginRequiredMixin, PermissionRequiredMixin,
                 UpdateViewContext):
    perms = 'workshops.change_host'
    model = Host
    form_class = HostForm
    slug_field = 'domain'
    slug_url_kwarg = 'host_domain'
    template_name = 'workshops/generic_form.html'


@login_required
@permission_required('workshops.delete_host', raise_exception=True)
def host_delete(request, host_domain):
    """Delete specific host."""
    try:
        host = get_object_or_404(Host, domain=host_domain)
        host.delete()
        messages.success(request, 'Host was deleted successfully.')
        return redirect(reverse('all_hosts'))
    except ProtectedError as e:
        return _failed_to_delete(request, host, e.protected_objects)


@login_required
@permission_required(['workshops.add_membership', 'workshops.change_host'],
                     raise_exception=True)
def membership_create(request, host_domain):
    host = get_object_or_404(Host, domain=host_domain)
    form = MembershipForm(initial={'host': host})

    if request.method == "POST":
        form = MembershipForm(request.POST)
        if form.is_valid():
            form.save()

            messages.success(request,
                             'Membership was successfully added to the host')

            return redirect(reverse('host_details', args=[host.domain]))

    context = {
        'title': 'New membership for host {}'.format(host),
        'form': form,
        'form_helper': bootstrap_helper,
    }
    return render(request, 'workshops/generic_form.html', context)


class MembershipUpdate(LoginRequiredMixin, PermissionRequiredMixin,
                       UpdateViewContext):
    perms = 'workshops.change_membership'
    model = Membership
    form_class = MembershipForm
    pk_url_kwarg = 'membership_id'
    template_name = 'workshops/generic_form.html'

    def get_success_url(self):
        return reverse('host_details', args=[self.object.host.domain])


@login_required
@permission_required('workshops.delete_membership', raise_exception=True)
def membership_delete(request, membership_id):
    """Delete specific membership."""
    try:
        membership = get_object_or_404(Membership, pk=membership_id)
        host = membership.host
        membership.delete()
        messages.success(request, 'Membership was deleted successfully.')
        return redirect(reverse('host_details', args=[host.domain]))
    except ProtectedError as e:
        return _failed_to_delete(request, host, e.protected_objects)



#------------------------------------------------------------

AIRPORT_FIELDS = ['iata', 'fullname', 'country', 'latitude', 'longitude']


@login_required
def all_airports(request):
    '''List all airports.'''
    filter = AirportFilter(request.GET, queryset=Airport.objects.all())
    airports = _get_pagination_items(request, filter)
    context = {'title' : 'All Airports',
               'all_airports' : airports,
               'filter': filter,
               'form_helper': bootstrap_helper_filter}
    return render(request, 'workshops/all_airports.html', context)


@login_required
def airport_details(request, airport_iata):
    '''List details of a particular airport.'''
    airport = get_object_or_404(Airport, iata=airport_iata)
    context = {'title' : 'Airport {0}'.format(airport),
               'airport' : airport}
    return render(request, 'workshops/airport.html', context)


class AirportCreate(LoginRequiredMixin, PermissionRequiredMixin,
                    CreateViewContext):
    perms = 'workshops.add_airport'
    model = Airport
    fields = AIRPORT_FIELDS
    template_name = 'workshops/generic_form.html'


class AirportUpdate(LoginRequiredMixin, PermissionRequiredMixin,
                    UpdateViewContext):
    perms = 'workshops.change_airport'
    model = Airport
    fields = AIRPORT_FIELDS
    slug_field = 'iata'
    slug_url_kwarg = 'airport_iata'
    template_name = 'workshops/generic_form.html'


@login_required
@permission_required('workshops.delete_airport', raise_exception=True)
def airport_delete(request, airport_iata):
    """Delete specific airport."""
    try:
        airport = get_object_or_404(Airport, iata=airport_iata)
        airport.delete()
        messages.success(request, 'Airport was deleted successfully.')
        return redirect(reverse('all_airports'))
    except ProtectedError as e:
        return _failed_to_delete(request, airport, e.protected_objects)

#------------------------------------------------------------


@login_required
def all_persons(request):
    '''List all persons.'''

    filter = PersonFilter(
        request.GET,
        queryset=Person.objects.all().defer('notes')  # notes are too large
    )
    # faster method
    instructors = Badge.objects.instructor_badges() \
                               .values_list('person', flat=True)
    persons = _get_pagination_items(request, filter)
    context = {'title' : 'All Persons',
               'all_persons' : persons,
               'instructors': instructors,
               'filter': filter,
               'form_helper': bootstrap_helper_filter}
    return render(request, 'workshops/all_persons.html', context)


@login_required
def person_details(request, person_id):
    '''List details of a particular person.'''
    person = get_object_or_404(Person, id=person_id)
    awards = person.award_set.all()
    tasks = person.task_set.all()
    lessons = person.lessons.all()
    domains = person.domains.all()
    context = {
        'title': 'Person {0}'.format(person),
        'person': person,
        'awards': awards,
        'tasks': tasks,
        'lessons': lessons,
        'domains': domains,
    }
    return render(request, 'workshops/person.html', context)


@login_required
def person_bulk_add_template(request):
    ''' Dynamically generate a CSV template that can be used to bulk-upload
    people.

    See https://docs.djangoproject.com/en/1.7/howto/outputting-csv/#using-the-python-csv-library
    '''
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename=BulkPersonAddTemplate.csv'

    writer = csv.writer(response)
    writer.writerow(Person.PERSON_TASK_UPLOAD_FIELDS)
    return response


@login_required
@permission_required('workshops.add_person', raise_exception=True)
def person_bulk_add(request):
    if request.method == 'POST':
        form = PersonBulkAddForm(request.POST, request.FILES)
        if form.is_valid():
            charset = request.FILES['file'].charset or settings.DEFAULT_CHARSET
            stream = io.TextIOWrapper(request.FILES['file'].file, charset)
            try:
                persons_tasks, empty_fields = upload_person_task_csv(stream)
            except csv.Error as e:
                messages.add_message(
                    request, messages.ERROR,
                    "Error processing uploaded .CSV file: {}".format(e))
            except UnicodeDecodeError as e:
                messages.add_message(
                    request, messages.ERROR,
                    "Please provide a file in {} encoding."
                    .format(charset))
            else:
                if empty_fields:
                    msg_template = ("The following required fields were not"
                                    " found in the uploaded file: {}")
                    msg = msg_template.format(', '.join(empty_fields))
                    messages.add_message(request, messages.ERROR, msg)
                else:
                    # instead of insta-saving, put everything into session
                    # then redirect to confirmation page which in turn saves
                    # the data
                    request.session['bulk-add-people'] = persons_tasks
                    return redirect('person_bulk_add_confirmation')

    else:
        form = PersonBulkAddForm()

    context = {
        'title': 'Bulk Add People',
        'form': form,
        'charset': settings.DEFAULT_CHARSET,
    }
    return render(request, 'workshops/person_bulk_add_form.html', context)


@login_required
@permission_required('workshops.add_person', raise_exception=True)
def person_bulk_add_confirmation(request):
    """
    This view allows for manipulating and saving session-stored upload data.
    """
    persons_tasks = request.session.get('bulk-add-people')

    # if the session is empty, add message and redirect
    if not persons_tasks:
        messages.warning(request, "Could not locate CSV data, please try the upload again.")
        return redirect('person_bulk_add')

    if request.method == 'POST':
        # update values if user wants to change them
        personals = request.POST.getlist("personal")
        families = request.POST.getlist("family")
        usernames = request.POST.getlist("username")
        emails = request.POST.getlist("email")
        events = request.POST.getlist("event")
        roles = request.POST.getlist("role")
        data_update = zip(personals, families, usernames, emails, events,
                          roles)
        for k, record in enumerate(data_update):
            personal, family, username, email, event, role = record
            # "field or None" converts empty strings to None values
            persons_tasks[k] = {
                'personal': personal,
                'family': family,
                'username': username,
                'email': email or None
            }
            # when user wants to drop related event they will send empty string
            # so we should unconditionally accept new value for event even if
            # it's an empty string
            persons_tasks[k]['event'] = event
            persons_tasks[k]['role'] = role
            persons_tasks[k]['errors'] = None  # reset here

        # save updated data to the session
        request.session['bulk-add-people'] = persons_tasks

        # check if user wants to verify or save, or cancel
        if request.POST.get('verify', None):
            # if there's "verify" in POST, then do only verification
            any_errors = verify_upload_person_task(persons_tasks)
            if any_errors:
                messages.add_message(request, messages.ERROR,
                                     "Please make sure to fix all errors "
                                     "listed below.")

            context = {'title': 'Confirm uploaded data',
                       'persons_tasks': persons_tasks,
                       'any_errors': any_errors}
            return render(request, 'workshops/person_bulk_add_results.html',
                          context)

        # there must be "confirm" and no "cancel" in POST in order to save
        elif (request.POST.get('confirm', None) and
              not request.POST.get('cancel', None)):
            try:
                # verification now makes something more than database
                # constraints so we should call it first
                verify_upload_person_task(persons_tasks)
                persons_created, tasks_created = \
                    create_uploaded_persons_tasks(persons_tasks)
            except (IntegrityError, ObjectDoesNotExist, InternalError) as e:
                messages.add_message(request, messages.ERROR,
                                     "Error saving data to the database: {}. "
                                     "Please make sure to fix all errors "
                                     "listed below.".format(e))
                any_errors = verify_upload_person_task(persons_tasks)
                context = {'title': 'Confirm uploaded data',
                           'persons_tasks': persons_tasks,
                           'any_errors': any_errors}
                return render(request,
                              'workshops/person_bulk_add_results.html',
                              context, status=400)

            else:
                request.session['bulk-add-people'] = None
                messages.add_message(
                    request, messages.SUCCESS,
                    'Successfully created {0} persons and {1} tasks.'
                    .format(len(persons_created), len(tasks_created))
                )
                return redirect('person_bulk_add')

        else:
            # any "cancel" or no "confirm" in POST cancels the upload
            request.session['bulk-add-people'] = None
            return redirect('person_bulk_add')

    else:
        # alters persons_tasks via reference
        any_errors = verify_upload_person_task(persons_tasks)

        context = {'title': 'Confirm uploaded data',
                   'persons_tasks': persons_tasks,
                   'any_errors': any_errors}
        return render(request, 'workshops/person_bulk_add_results.html',
                      context)


class PersonCreate(LoginRequiredMixin, PermissionRequiredMixin,
                   CreateViewContext):
    perms = 'workshops.add_person'
    model = Person
    form_class = PersonForm
    template_name = 'workshops/generic_form.html'

    def form_valid(self, form):
        """Person.lessons uses an intermediary model so we need to manually add
        objects of that model.

        See more here: http://stackoverflow.com/a/15745652"""
        self.object = form.save(commit=False)  # don't save M2M fields

        # Need to save that object because of commit=False previously.
        # This doesn't save our troublesome M2M field.
        self.object.save()

        # saving intermediary M2M model: Qualification
        for lesson in form.cleaned_data['lessons']:
            Qualification.objects.create(lesson=lesson, person=self.object)

        # Important: we need to use ModelFormMixin.form_valid() here!
        # But by doing so we omit SuccessMessageMixin completely, so we need to
        # simulate it.  The code below is almost identical to
        # SuccessMessageMixin.form_valid().
        response = super(ModelFormMixin, self).form_valid(form)
        success_message = self.get_success_message(form.cleaned_data)
        if success_message:
            messages.success(self.request, success_message)
        return response


@login_required
@permission_required(['workshops.change_person', 'workshops.add_award',
                      'workshops.add_task'],
                     raise_exception=True)
def person_edit(request, person_id):
    person = get_object_or_404(Person, id=person_id)
    awards = person.award_set.order_by('badge__name')
    tasks = person.task_set.order_by('-event__slug')

    person_form = PersonForm(prefix='person', instance=person)
    award_form = PersonAwardForm(prefix='award', initial={
        'awarded': datetime.date.today(),
        'person': person,
    })
    task_form = PersonTaskForm(prefix='task', initial={'person': person})

    if request.method == 'POST':
        # check which form was submitted
        if 'award-badge' in request.POST:
            award_form = PersonAwardForm(request.POST, prefix='award')

            if award_form.is_valid():
                award = award_form.save()

                messages.success(
                    request,
                    '{person} was awarded {badge} badge.'.format(
                        person=str(person),
                        badge=award.badge.title,
                    ),
                    extra_tags='awards',
                )

                # to reset the form values
                return redirect(request.path)

            else:
                messages.error(request, 'Fix errors in the award form.',
                               extra_tags='awards')

        elif 'task-role' in request.POST:
            task_form = PersonTaskForm(request.POST, prefix='task')

            if task_form.is_valid():
                task = task_form.save()

                messages.success(
                    request,
                    '{person} was added a role {role} during {event} event.'
                    .format(
                        person=str(person),
                        role=task.role.name,
                        event=task.event.slug,
                    ),
                    extra_tags='tasks',
                )

                # to reset the form values
                return redirect(request.path)

            else:
                messages.error(request, 'Fix errors in the task form.',
                               extra_tags='tasks')

        else:
            person_form = PersonForm(request.POST, prefix='person',
                                     instance=person)
            if person_form.is_valid():
                lessons = person_form.cleaned_data['lessons']

                # remove existing Qualifications for user
                Qualification.objects.filter(person=person).delete()

                # add new Qualifications
                for lesson in lessons:
                    Qualification.objects.create(person=person, lesson=lesson)

                # don't save related lessons
                del person_form.cleaned_data['lessons']

                person = person_form.save()

                messages.success(
                    request,
                    '{name} was updated successfully.'.format(
                        name=str(person),
                    ),
                )

                return redirect(person)

            else:
                messages.error(request, 'Fix errors below.')

    # two separate forms on one page
    context = {'title': 'Edit Person {0}'.format(str(person)),
               'person_form': person_form,
               'object': person,
               'model': Person,
               'awards': awards,
               'award_form': award_form,
               'tasks': tasks,
               'task_form': task_form,
               'form_helper': bootstrap_helper,
               'form_helper_with_add': bootstrap_helper_with_add,
               }
    return render(request, 'workshops/person_edit_form.html', context)


@login_required
@permission_required('workshops.delete_person', raise_exception=True)
def person_delete(request, person_id):
    """Delete specific person."""
    try:
        person = get_object_or_404(Person, pk=person_id)
        person.delete()

        messages.success(request, 'Person was deleted successfully.')
        return redirect(reverse('all_persons'))
    except ProtectedError as e:
        return _failed_to_delete(request, person, e.protected_objects)


class PersonPermissions(LoginRequiredMixin, PermissionRequiredMixin,
                        UpdateViewContext):
    perms = 'workshops.change_person'
    model = Person
    form_class = PersonPermissionsForm
    pk_url_kwarg = 'person_id'
    template_name = 'workshops/generic_form.html'


@login_required
def person_password(request, person_id):
    user = get_object_or_404(Person, pk=person_id)

    # Either the user requests change of their own password, or someone with
    # permission for changing person does.
    if not ((request.user == user) or
            (request.user.has_perm('workshops.change_person'))):
        raise PermissionDenied

    Form = PasswordChangeForm
    if request.user.is_superuser:
        Form = SetPasswordForm
    elif request.user.pk != user.pk:
        # non-superuser can only change their own password, not someone else's
        raise PermissionDenied

    if request.method == 'POST':
        form = Form(user, request.POST)
        if form.is_valid():
            form.save()  # saves the password for the user

            update_session_auth_hash(request, form.user)

            messages.success(request, 'Password was changed successfully.')

            return redirect(reverse('person_details', args=[user.id]))

        else:
            messages.error(request, 'Fix errors below.')
    else:
        form = Form(user)

    return render(request, 'workshops/generic_form.html', {
        'form': form,
        'model': Person,
        'object': user,
        'form_helper': bootstrap_helper,
        'title': 'Change password',
    })


@login_required
@permission_required(['workshops.add_person', 'workshops.delete_person'],
                     raise_exception=True)
def person_merge(request):
    'Merge information from one Person into another (in case of duplicates).'

    if request.method == 'POST':
        form = PersonMergeForm(request.POST)
        if form.is_valid():
            request.session['person_from'] = form.cleaned_data['person_from'] \
                                                 .pk
            request.session['person_to'] = form.cleaned_data['person_to'].pk
            return redirect('person_merge_confirmation')
        else:
            messages.error(request, 'Fix errors below.')
    else:
        if 'person_from' in request.session:
            del request.session['person_from']
        if 'person_to' in request.session:
            del request.session['person_to']
        form = PersonMergeForm()

    context = {'title': 'Merge Persons',
               'person_merge_form': form,
               'form_helper': bootstrap_helper}
    return render(request, 'workshops/person_merge_form.html', context)


@login_required
@permission_required(['workshops.add_person', 'workshops.delete_person'],
                     raise_exception=True)
def person_merge_confirmation(request):
    '''Show what the merge will do and get confirmation.'''
    person_from = get_object_or_404(Person,
                                    pk=request.session.get('person_from'))
    person_to = get_object_or_404(Person,
                                  pk=request.session.get('person_to'))

    # Must not be the same person.
    if person_from == person_to:
        del request.session['person_from']
        del request.session['person_to']
        messages.warning(request, 'Cannot merge a person with themselves.')
        return redirect('person_merge')

    if "confirmed" in request.GET:
        merge_persons(person_from, person_to)
        messages.success(request,
                         'Merging {0} into {1}'.format(person_from,
                                                       person_to))
        return redirect('person_merge')

    else:
        context = {'title': 'Confirm merge',
                   'person_from': person_from,
                   'person_to': person_to}
        return render(request, 'workshops/person_merge_confirm.html', context)

#------------------------------------------------------------

@login_required
def all_events(request):
    '''List all events.'''
    filter = EventFilter(
        request.GET,
        queryset=Event.objects.all().defer('notes')  # notes are too large
                                    .prefetch_related('host', 'tags'),
    )
    events = _get_pagination_items(request, filter)
    context = {'title' : 'All Events',
               'all_events' : events,
               'filter': filter,
               'form_helper': bootstrap_helper_filter}
    return render(request, 'workshops/all_events.html', context)


@login_required
@permission_required('workshops.add_todoitem', raise_exception=True)
def event_details(request, event_ident):
    '''List details of a particular event.'''
    try:
        event = Event.get_by_ident(event_ident)
    except Event.DoesNotExist:
        raise Http404('Event matching query does not exist.')

    tasks = Task.objects.filter(event__id=event.id) \
                        .select_related('person', 'role') \
                        .order_by('role__name')
    todos = event.todoitem_set.all()
    todo_form = SimpleTodoForm(prefix='todo', initial={
        'event': event,
    })

    if request.method == "POST":
        todo_form = SimpleTodoForm(request.POST, prefix='todo', initial={
            'event': event,
        })
        if todo_form.is_valid():
            todo = todo_form.save()

            messages.success(
                request,
                'New TODO {todo} was added to the event {event}.'.format(
                    todo=str(todo),
                    event=event.get_ident(),
                ),
                extra_tags='newtodo',
            )
            return redirect(reverse(event_details, args=[event_ident, ]))
        else:
            messages.error(request, 'Fix errors in the TODO form.',
                           extra_tags='todos')

    person_lookup_form = AdminLookupForm()
    if event.assigned_to:
        person_lookup_form = AdminLookupForm(
            initial={'person': event.assigned_to}
        )

    person_lookup_helper = BootstrapHelper()
    person_lookup_helper.form_action = reverse('event_assign',
                                               args=[event_ident])

    context = {
        'title': 'Event {0}'.format(event),
        'event': event,
        'tasks': tasks,
        'todo_form': todo_form,
        'todos': todos,
        'helper': bootstrap_helper,
        'today': datetime.date.today(),
        'person_lookup_form': person_lookup_form,
        'person_lookup_helper': person_lookup_helper,
    }
    return render(request, 'workshops/event.html', context)


@login_required
def validate_event(request, event_ident):
    '''Check the event's home page *or* the specified URL (for testing).'''
    try:
        event = Event.get_by_ident(event_ident)
    except Event.DoesNotExist:
        raise Http404('Event matching query does not exist.')

    page_url = request.GET.get('url', None)  # for manual override
    if page_url is None:
        page_url = event.url

    page_url = page_url.strip()

    error_messages = []

    try:
        # fetch page
        response = requests.get(page_url)
        response.raise_for_status()  # assert it's 200 OK
        content = response.text

        # find tags
        tags = find_tags_on_event_website(content)

        if 'slug' not in tags:
            # there are no HTML tags, so let's try the old method
            page_url, _ = generate_url_to_event_index(page_url)

            # fetch page
            response = requests.get(page_url)

            if response.status_code == 200:
                # don't throw errors for pages we fall back to
                content = response.text
                tags = find_tags_on_event_index(page_url)

        # validate them
        error_messages = validate_tags_from_event_website(tags)

    except WrongWorkshopURL as e:
        error_messages.append(str(e))

    except requests.exceptions.HTTPError as e:
        error_messages.append(
            'Request for "{0}" returned status code {1}'
            .format(page_url, e.response.status_code)
        )

    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
        error_messages.append("Network connection error.")

    context = {
        'title': 'Validate Event {0}'.format(event),
        'event': event,
        'page': page_url,
        'error_messages': error_messages,
    }
    return render(request, 'workshops/validate_event.html', context)


class EventCreate(LoginRequiredMixin, PermissionRequiredMixin,
                  CreateViewContext):
    perms = 'workshops.add_event'
    model = Event
    form_class = EventForm
    template_name = 'workshops/event_create_form.html'


@login_required
@permission_required(['workshops.change_event', 'workshops.add_task'],
                     raise_exception=True)
def event_edit(request, event_ident):
    try:
        event = Event.get_by_ident(event_ident)
        tasks = event.task_set.order_by('role__name')
    except ObjectDoesNotExist:
        raise Http404("No event found matching the query.")

    event_form = EventForm(prefix='event', instance=event)
    task_form = TaskForm(prefix='task', initial={
        'event': event,
    })

    if request.method == 'POST':
        # check which form was submitted
        if "task-role" in request.POST:
            task_form = TaskForm(request.POST, prefix='task')

            if task_form.is_valid():
                task = task_form.save()

                messages.success(
                    request,
                    '{event} was added a new task "{task}".'.format(
                        event=str(event),
                        task=str(task),
                    ),
                )

                # if event.attendance is lower than number of learners, then
                # update the attendance
                update_event_attendance_from_tasks(event)

                # to reset the form values
                return redirect(request.path)

            else:
                messages.error(request, 'Fix errors below.')

        else:
            event_form = EventForm(request.POST, prefix='event',
                                   instance=event)
            if event_form.is_valid():
                event = event_form.save()

                messages.success(
                    request,
                    '{name} was updated successfully.'.format(
                        name=str(event),
                    ),
                )

                # if event.attendance is lower than number of learners, then
                # update the attendance
                update_event_attendance_from_tasks(event)

                return redirect(event)

            else:
                messages.error(request, 'Fix errors below.')

    context = {'title': 'Edit Event {0}'.format(event.get_ident()),
               'event_form': event_form,
               'object': event,
               'model': Event,
               'tasks': tasks,
               'task_form': task_form,
               'form_helper': bootstrap_helper,
               'form_helper_with_add': bootstrap_helper_with_add,
               }
    return render(request, 'workshops/event_edit_form.html', context)


@login_required
@permission_required('workshops.delete_event', raise_exception=True)
def event_delete(request, event_ident):
    """Delete event, its tasks and related awards."""
    try:
        event = Event.get_by_ident(event_ident)
        event.delete()

        messages.success(request,
                         'Event and its tasks were deleted successfully.')
        return redirect(reverse('all_events'))
    except ObjectDoesNotExist:
        raise Http404("No event found matching the query.")
    except ProtectedError as e:
        return _failed_to_delete(request, event, e.protected_objects)


@login_required
@require_POST
def event_import(request):
    """Read tags from remote URL and return them as JSON.

    This is used to read tags from workshop website and then fill up fields
    on event_create form."""

    url = request.POST['url'].strip()
    try:
        # fetch page
        response = requests.get(url)
        response.raise_for_status()  # assert it's 200 OK
        content = response.text

        # find tags
        tags = find_tags_on_event_website(content)

        if 'slug' not in tags:
            # there are no HTML tags, so let's try the old method
            index_url, repository = generate_url_to_event_index(url)

            # fetch page
            response = requests.get(index_url)

            if response.status_code == 200:
                # don't throw errors for pages we fall back to
                content = response.text
                tags = find_tags_on_event_index(content)

                if 'slug' not in tags:
                    tags['slug'] = repository

        # normalize (parse) them
        tags = parse_tags_from_event_website(tags)

        return JsonResponse(tags)

    except requests.exceptions.HTTPError as e:
        return HttpResponseBadRequest(
            'Request for "{0}" returned status code {1}.'
            .format(url, e.response.status_code)
        )

    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
        return HttpResponseBadRequest('Network connection error.')

    except WrongWorkshopURL as e:
        return HttpResponseBadRequest(str(e))

    except KeyError:
        return HttpResponseBadRequest('Missing or wrong "url" POST parameter.')


@login_required
@permission_required('workshops.change_event', raise_exception=True)
def event_assign(request, event_ident, person_id=None):
    """Set event.assigned_to. See `_assign` docstring for more information."""
    try:
        event = Event.get_by_ident(event_ident)

        _assign(request, event, person_id)

        return redirect(reverse('event_details', args=[event.get_ident()]))

    except Event.DoesNotExist:
        raise Http404("No event found matching the query.")

#------------------------------------------------------------

@login_required
def all_tasks(request):
    '''List all tasks.'''

    filter = TaskFilter(
        request.GET,
        queryset=Task.objects.all().select_related('event', 'person', 'role')
                                   .defer('person__notes', 'event__notes')
    )
    tasks = _get_pagination_items(request, filter)
    context = {'title' : 'All Tasks',
               'all_tasks' : tasks,
               'filter': filter,
               'form_helper': bootstrap_helper_filter}
    return render(request, 'workshops/all_tasks.html', context)


@login_required
def task_details(request, task_id):
    '''List details of a particular task.'''
    task = get_object_or_404(Task, pk=task_id)
    context = {'title' : 'Task {0}'.format(task),
               'task' : task}
    return render(request, 'workshops/task.html', context)


@login_required
@permission_required('workshops.delete_task', raise_exception=True)
def task_delete(request, task_id, event_ident=None):
    '''Delete a task. This is used on the event edit page'''
    t = get_object_or_404(Task, pk=task_id)
    t.delete()

    messages.success(request, 'Task was deleted successfully.')

    if event_ident:
        return redirect(event_edit, event_ident)
    return redirect(all_tasks)


class TaskCreate(LoginRequiredMixin, PermissionRequiredMixin,
                 CreateViewContext):
    perms = 'workshops.add_task'
    model = Task
    form_class = TaskFullForm
    template_name = 'workshops/generic_form.html'


class TaskUpdate(LoginRequiredMixin, PermissionRequiredMixin,
                 UpdateViewContext):
    perms = 'workshops.change_task'
    model = Task
    form_class = TaskFullForm
    pk_url_kwarg = 'task_id'
    template_name = 'workshops/generic_form.html'

#------------------------------------------------------------


@login_required
@permission_required('workshops.delete_award', raise_exception=True)
def award_delete(request, award_id, person_id=None):
    """Delete an award. This is used on the person edit page."""
    award = get_object_or_404(Award, pk=award_id)
    badge_name = award.badge.name
    award.delete()

    messages.success(request, 'Award was deleted successfully.',
                     extra_tags='awards')

    if person_id:
        # if a second form of URL, then return back to person edit page
        return redirect(person_edit, person_id)

    return redirect(reverse(badge_details, args=[badge_name]))


#------------------------------------------------------------

@login_required
def all_badges(request):
    '''List all badges.'''

    badges = Badge.objects.order_by('name').annotate(num_awarded=Count('award'))
    context = {'title' : 'All Badges',
               'all_badges' : badges}
    return render(request, 'workshops/all_badges.html', context)


@login_required
def badge_details(request, badge_name):
    '''List details of a particular event.'''

    badge = get_object_or_404(Badge, name=badge_name)

    initial = {
        'badge': badge,
        'awarded': datetime.date.today()
    }

    if request.method == 'GET':
        form = BadgeAwardForm(initial=initial)

    elif request.method == 'POST':
        form = BadgeAwardForm(request.POST, initial=initial)

        if request.user.has_perm('workshops.add_award'):
            if form.is_valid():
                form.save()
        else:
            messages.error(request,
                           'You don\'t have permissions to award a badge.')

    awards = badge.award_set.all()
    awards = _get_pagination_items(request, awards)

    context = {'title': 'Badge {0}'.format(badge),
               'badge': badge,
               'awards': awards,
               'form': form,
               'form_helper': bootstrap_helper}
    return render(request, 'workshops/badge.html', context)


#------------------------------------------------------------


@login_required
def instructors(request):
    '''Search for instructors.'''
    instructor_badges = Badge.objects.instructor_badges()
    instructors = Person.objects.filter(badges__in=instructor_badges) \
                                .filter(airport__isnull=False) \
                                .select_related('airport') \
                                .prefetch_related('lessons')
    instructors = instructors.annotate(
        num_taught=Count(
            Case(
                When(
                    task__role__name='instructor',
                    then=Value(1)
                ),
                output_field=IntegerField()
            )
        )
    )
    form = InstructorsForm()

    lessons = list()

    if 'submit' in request.GET:
        form = InstructorsForm(request.GET)
        if form.is_valid():
            data = form.cleaned_data

            if data['lessons']:
                lessons = data['lessons']
                # this has to be in a loop to match a *subset* of lessons,
                # not any lesson within the list (as it would be with
                # `.filter(lessons_in=lessons)`)
                for lesson in lessons:
                    instructors = instructors.filter(
                        qualification__lesson=lesson
                    )

            if data['airport']:
                x = data['airport'].latitude
                y = data['airport'].longitude
                # using Euclidean distance just because it's faster and easier
                complex_F = ((F('airport__latitude') - x) ** 2
                             + (F('airport__longitude') - y) ** 2)
                instructors = instructors.annotate(distance=complex_F) \
                                         .order_by('distance', 'family')

            if data['latitude'] and data['longitude']:
                x = data['latitude']
                y = data['longitude']
                # using Euclidean distance just because it's faster and easier
                complex_F = ((F('airport__latitude') - x) ** 2
                             + (F('airport__longitude') - y) ** 2)
                instructors = instructors.annotate(distance=complex_F) \
                                         .order_by('distance', 'family')

            if data['country']:
                instructors = instructors.filter(
                    airport__country__in=data['country']
                ).order_by('family')

            if data['gender']:
                instructors = instructors.filter(gender=data['gender'])

            if data['instructor_badges']:
                for badge in data['instructor_badges']:
                    instructors = instructors.filter(badges__name=badge)

    instructors = _get_pagination_items(request, instructors)
    context = {
        'title': 'Find Instructors',
        'form': form,
        'persons': instructors,
        'lessons': lessons,
    }
    return render(request, 'workshops/instructors.html', context)

#------------------------------------------------------------


@login_required
def search(request):
    '''Search the database by term.'''

    term, hosts, events, persons, airports = '', None, None, None, None

    if request.method == 'POST':
        form = SearchForm(request.POST)
        if form.is_valid():
            term = form.cleaned_data['term']
            tokens = re.split('\s+', term)
            results = list()

            if form.cleaned_data['in_hosts']:
                hosts = Host.objects.filter(
                    Q(domain__contains=term) |
                    Q(fullname__contains=term) |
                    Q(notes__contains=term)) \
                    .order_by('fullname')
                results += list(hosts)

            if form.cleaned_data['in_events']:
                events = Event.objects.filter(
                    Q(slug__contains=term) |
                    Q(notes__contains=term) |
                    Q(host__domain__contains=term) |
                    Q(host__fullname__contains=term) |
                    Q(url__contains=term) |
                    Q(contact__contains=term) |
                    Q(venue__contains=term) |
                    Q(address__contains=term)
                ).order_by('-slug')
                results += list(events)

            if form.cleaned_data['in_persons']:
                # if user searches for two words, assume they mean a person
                # name
                if len(tokens) == 2:
                    name1, name2 = tokens
                    complex_q = (
                        Q(personal__contains=name1) & Q(family__contains=name2)
                    ) | (
                        Q(personal__contains=name2) & Q(family__contains=name1)
                    ) | Q(email__contains=term) | Q(github__contains=term)
                    persons = Person.objects.filter(complex_q)
                else:
                    persons = Person.objects.filter(
                        Q(personal__contains=term) |
                        Q(family__contains=term) |
                        Q(email__contains=term) |
                        Q(github__contains=term)) \
                        .order_by('family')
                results += list(persons)

            if form.cleaned_data['in_airports']:
                airports = Airport.objects.filter(
                    Q(iata__contains=term) |
                    Q(fullname__contains=term)) \
                    .order_by('iata')
                results += list(airports)

            # only 1 record found? Let's move to it immediately
            if len(results) == 1:
                return redirect(results[0].get_absolute_url())

    # if a GET (or any other method) we'll create a blank form
    else:
        form = SearchForm()

    context = {'title' : 'Search',
               'form': form,
               'form_helper': bootstrap_helper,
               'term' : term,
               'hosts' : hosts,
               'events' : events,
               'persons' : persons,
               'airports' : airports}
    return render(request, 'workshops/search.html', context)

#------------------------------------------------------------

@login_required
def debrief(request):
    '''Show who taught between begin_date and end_date.'''

    tasks = None

    start_date = end_date = None

    tags = Tag.objects.filter(name__in=['stalled', 'unresponsive'])

    if request.method == 'POST':
        form = DebriefForm(request.POST)
        if form.is_valid():
            start_date = form.cleaned_data['begin_date']
            end_date = form.cleaned_data['end_date']
            tasks = Task.objects.filter(
                event__start__gte=start_date,
                event__end__lte=end_date,
                role__name='instructor',
                person__may_contact=True,
            ).exclude(event__tags=tags).order_by('event', 'person', 'role') \
             .select_related('person', 'event', 'role')

    else:
        # if a GET (or any other method) we'll create a blank form
        form = DebriefForm()

    context = {'title': 'Debrief',
               'form': form,
               'form_helper': bootstrap_helper,
               'all_tasks': tasks,
               'start_date': start_date,
               'end_date': end_date}
    return render(request, 'workshops/debrief.html', context)

#------------------------------------------------------------

@login_required
def export_badges(request):
    title = 'Badges'
    json_link = reverse('api:export-badges', kwargs={'format': 'json'})
    yaml_link = reverse('api:export-badges', kwargs={'format': 'yaml'})
    context = {
        'title': title,
        'json_link': json_link,
        'yaml_link': yaml_link,
    }
    return render(request, 'workshops/export.html', context)


@login_required
def export_instructors(request):
    title = 'Instructor Locations'
    json_link = reverse('api:export-instructors', kwargs={'format': 'json'})
    yaml_link = reverse('api:export-instructors', kwargs={'format': 'yaml'})
    context = {
        'title': title,
        'json_link': json_link,
        'yaml_link': yaml_link,
    }
    return render(request, 'workshops/export.html', context)


@login_required
def export_members(request):
    title = 'SCF Members'
    json_link = reverse('api:export-members', kwargs={'format': 'json'})
    yaml_link = reverse('api:export-members', kwargs={'format': 'yaml'})
    context = {
        'title': title,
        'json_link': json_link,
        'yaml_link': yaml_link,
    }
    return render(request, 'workshops/export.html', context)

#------------------------------------------------------------

@login_required
def workshops_over_time(request):
    '''Export CSV of count of workshops vs. time.'''

    data = dict(Event.objects
                     .past_events()
                     .values_list('start')
                     .annotate(Count('id')))
    return _time_series(request, data, 'Workshop over time')


@login_required
def learners_over_time(request):
    '''Export CSV of count of learners vs. time.'''

    data = dict(Event.objects
                     .past_events()
                     .values_list('start')
                     .annotate(Sum('attendance')))
    return _time_series(request, data, 'Learners over time')


@login_required
def instructors_over_time(request):
    '''Export CSV of count of instructors vs. time.'''

    badges = Badge.objects.instructor_badges()
    data = dict(Award.objects.filter(badge__in=badges)
                     .values_list('awarded')
                     .annotate(Count('person__id')))
    return _time_series(request, data, 'Instructors over time')


@login_required
def instructor_num_taught(request):
    '''Export CSV of how often instructors have taught.'''

    badges = Badge.objects.instructor_badges()
    awards = Award.objects.filter(badge__in=badges).annotate(
        num_taught=Count(
            Case(
                When(
                    person__task__role__name='instructor',
                    then=Value(1)
                ),
                output_field=IntegerField()
            )
        )
    ).select_related('person', 'person__airport') \
     .filter(person__may_contact=True).order_by('-num_taught', 'awarded')
    context = {'title': 'Frequency of Instruction',
               'awards': awards}
    return render(request, 'workshops/instructor_num_taught.html', context)


@login_required
def workshop_issues(request):
    '''Display workshops in the database whose records need attention.'''

    events = Event.objects.past_events().filter(
        Q(attendance=None) | Q(attendance=0) |
        Q(country=None) |
        Q(venue=None) | Q(venue__exact='') |
        Q(address=None) | Q(address__exact='') |
        Q(latitude=None) | Q(longitude=None) |
        Q(start__gt=F('end'))
    )

    assigned_to, is_admin = assignment_selection(request)

    if assigned_to == 'me':
        events = events.filter(assigned_to=request.user)

    elif assigned_to == 'noone':
        events = events.filter(assigned_to=None)

    elif assigned_to == 'all':
        # no filtering
        pass

    for e in events:
        e.missing_attendance_ = (not e.attendance)
        e.missing_location_ = (
            not e.country or not e.venue or not e.address or not e.latitude or
            not e.longitude
        )
        e.bad_dates_ = e.start and e.end and (e.start > e.end)

    context = {
        'title': 'Workshops with Issues',
        'events': events,
        'is_admin': is_admin,
        'assigned_to': assigned_to,
    }
    return render(request, 'workshops/workshop_issues.html', context)


@login_required
def instructor_issues(request):
    '''Display instructors in the database who need attention.'''

    # Everyone who has a badge but needs attention.
    instructor_badges = Badge.objects.instructor_badges()
    instructors = Person.objects.filter(badges__in=instructor_badges) \
                                .filter(airport__isnull=True)

    # Everyone who's been in instructor training but doesn't yet have a badge.
    learner = Role.objects.get(name='learner')
    ttt = Tag.objects.get(name='TTT')
    stalled = Tag.objects.get(name='stalled')
    trainees = Task.objects \
        .filter(event__tags__in=[ttt], role=learner) \
        .exclude(person__badges__in=instructor_badges) \
        .order_by('person__family', 'person__personal', 'event__start') \
        .select_related('person', 'event')

    pending_instructors = trainees.exclude(event__tags=stalled)
    pending_instructors_person_ids = pending_instructors.values_list(
        'person__pk', flat=True,
    )

    stalled_instructors = trainees \
        .filter(event__tags=stalled) \
        .exclude(person__id__in=pending_instructors_person_ids)

    context = {
        'title': 'Instructors with Issues',
        'instructors': instructors,
        'pending': pending_instructors,
        'stalled': stalled_instructors,
    }
    return render(request, 'workshops/instructor_issues.html', context)


#------------------------------------------------------------


@login_required
def object_changes(request, revision_id):
    revision = Revision.objects.get(pk=revision_id)

    # we assume there's only one version per revision
    current_version = revision.version_set.all()[0]
    obj = current_version.object

    try:
        previous_version = get_for_object(obj) \
                                .filter(pk__lt=current_version.pk)[0]
        obj_prev = previous_version.object
    except IndexError:
        # first revision for an object
        previous_version = current_version
        obj_prev = obj

    context = {
        'object_prev': obj_prev,
        'object': obj,
        'previous_version': previous_version,
        'current_version': current_version,
        'revision': revision,
        'title': str(obj),
    }
    if obj.__class__ == Person:
        return render(request, 'workshops/person_diff.html', context)
    elif obj.__class__ == Event:
        return render(request, 'workshops/event_diff.html', context)
    else:
        context['verbose_name'] = obj._meta.verbose_name
        context['fields'] = [
            f for f in obj._meta.get_fields()
            if f.concrete and not f.is_relation
        ]
        return render(request, 'workshops/object_diff.html', context)

# ------------------------------------------------------------

def _get_pagination_items(request, all_objects):
    '''Select paginated items.'''

    # Get parameters.
    items = request.GET.get('items_per_page', ITEMS_PER_PAGE)
    if items != 'all':
        try:
            items = int(items)
        except ValueError:
            items = ITEMS_PER_PAGE
    else:
        # Show everything.
        items = all_objects.count()

    # Figure out where we are.
    page = request.GET.get('page')

    # Show selected items.
    paginator = Paginator(all_objects, items)

    # Select the pages.
    try:
        result = paginator.page(page)

    # If page is not an integer, deliver first page.
    except PageNotAnInteger:
        result = paginator.page(1)

    # If page is out of range, deliver last page of results.
    except EmptyPage:
        result = paginator.page(paginator.num_pages)

    return result


def _time_series(request, data, title):
    '''Prepare time-series data for display and render it.'''

    # Make sure addition will work.
    for key in data:
        if data[key] is None:
            data[key] = 0

    # Create running total.
    data = list(data.items())
    data.sort()
    for i in range(1, len(data)):
        data[i] = (data[i][0], data[i][1] + data[i-1][1])

    # Textualize and display.
    data = '\n'.join(['{0},{1}'.format(*d) for d in data])
    context = {'title': title,
               'data': data}
    return render(request, 'workshops/time_series.html', context)


def _failed_to_delete(request, object, protected_objects, back=None):
    context = {
        'title': 'Failed to delete',
        'back': back or object.get_absolute_url,
        'object': object,
        'refs': defaultdict(list),
    }

    for obj in protected_objects:
        # e.g. for model Award its plural name is 'awards'
        name = str(obj.__class__._meta.verbose_name_plural)
        context['refs'][name].append(obj)

    # this trick enables looping through defaultdict instance
    context['refs'].default_factory = None

    return render(request, 'workshops/failed_to_delete.html', context)

#------------------------------------------------------------


class SWCEventRequest(View):
    form_class = SWCEventRequestForm
    form_helper = bootstrap_helper_wider_labels
    page_title = 'Request a Software Carpentry Workshop'
    form_template = 'forms/workshop_swc_request.html'
    success_template = 'forms/workshop_request_confirm.html'

    def get(self, request, *args, **kwargs):
        form = self.form_class()
        context = {
            'title': self.page_title,
            'form': form,
            'form_helper': self.form_helper,
        }
        return render(request, self.form_template, context)

    def post(self, request, *args, **kwargs):
        form = self.form_class(request.POST)

        if form.is_valid():
            data = form.cleaned_data
            event_req = form.save()

            # prepare email notification
            recipients = settings.REQUEST_NOTIFICATIONS_RECIPIENTS
            subject = (
                '[{tag}] New workshop request: {affiliation}, {country}'
            ).format(
                tag=data['workshop_type'].upper(),
                country=event_req.country.name,
                affiliation=event_req.affiliation,
            )

            link = event_req.get_absolute_url()
            link_domain = settings.SITE_URL

            body_txt = get_template(
                'workshops/eventrequest_email_txt.html'
            ).render({
                'object': event_req,
                'link': link,
                'link_domain': link_domain,
            })

            body_html = get_template(
                'workshops/eventrequest_email_html.html'
            ).render({
                'object': event_req,
                'link': link,
                'link_domain': link_domain,
            })

            reply_to = (data['email'], )
            email = EmailMultiAlternatives(subject, body_txt, to=recipients,
                                           reply_to=reply_to)
            email.attach_alternative(body_html, 'text/html')

            # fail loudly so that admins know if something's wrong
            email.send(fail_silently=False)

            context = {
                'title': 'Thank you for requesting a workshop',
            }
            return render(request, self.success_template, context)
        else:
            messages.error(request, 'Fix errors below.')
            context = {
                'title': self.page_title,
                'form': form,
                'form_helper': self.form_helper,
            }
            return render(request, self.form_template, context)


class DCEventRequest(SWCEventRequest):
    form_class = DCEventRequestForm
    page_title = 'Request a Data Carpentry Workshop'
    form_template = 'forms/workshop_dc_request.html'


@login_required
def all_eventrequests(request):
    """List all event requests."""

    # Set initial value for the "active" radio select.  That's a hack, nothing
    # else worked...
    data = request.GET.copy()  # request.GET is immutable
    data['active'] = data.get('active', 'true')
    filter = EventRequestFilter(
        data,
        queryset=EventRequest.objects.all(),
    )
    eventrequests = _get_pagination_items(request, filter)
    context = {
        'title': 'Workshop requests',
        'requests': eventrequests,
        'filter': filter,
        'form_helper': bootstrap_helper_filter,
    }
    return render(request, 'workshops/all_eventrequests.html', context)


class EventRequestDetails(LoginRequiredMixin, DetailView):
    queryset = EventRequest.objects.all()
    context_object_name = 'object'
    template_name = 'workshops/eventrequest.html'
    pk_url_kwarg = 'request_id'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'Workshop request #{}'.format(self.get_object().pk)

        person_lookup_form = AdminLookupForm()
        if self.object.assigned_to:
            person_lookup_form = AdminLookupForm(
                initial={'person': self.object.assigned_to}
            )

        person_lookup_helper = BootstrapHelper()
        person_lookup_helper.form_action = reverse('eventrequest_assign',
                                                   args=[self.object.pk])

        context['person_lookup_form'] = person_lookup_form
        context['person_lookup_helper'] = person_lookup_helper
        return context


@login_required
@permission_required('workshops.change_eventrequest', raise_exception=True)
def eventrequest_discard(request, request_id):
    """Discard EventRequest, ie. set it to inactive."""
    eventrequest = get_object_or_404(EventRequest, active=True, pk=request_id)
    eventrequest.active = False
    eventrequest.save()

    messages.success(request,
                     'Workshop request was discarded successfully.')
    return redirect(reverse('all_eventrequests'))


@login_required
@permission_required(['workshops.change_eventrequest', 'workshops.add_event'],
                     raise_exception=True)
def eventrequest_accept(request, request_id):
    """Accept event request by creating a new event."""
    eventrequest = get_object_or_404(EventRequest, active=True, pk=request_id)
    form = EventForm()

    if request.method == 'POST':
        form = EventForm(request.POST)

        if form.is_valid():
            event = form.save()
            eventrequest.active = False
            eventrequest.save()
            return redirect(reverse('event_details',
                                    args=[event.get_ident()]))
        else:
            messages.error(request, 'Fix errors below.')

    context = {
        'object': eventrequest,
        'form': form,
    }
    return render(request, 'workshops/eventrequest_accept.html', context)


@login_required
@permission_required(['workshops.change_eventrequest'], raise_exception=True)
def eventrequest_assign(request, request_id, person_id=None):
    """Set eventrequest.assigned_to. See `_assign` docstring for more
    information."""
    event_req = get_object_or_404(EventRequest, pk=request_id)
    _assign(request, event_req, person_id)
    return redirect(reverse('eventrequest_details', args=[event_req.pk]))


def profileupdaterequest_create(request):
    """
    Profile update request form. Accessible to all users (no login required).

    This one is used when instructors want to change their information.
    """
    form = ProfileUpdateRequestForm()
    form_helper = bootstrap_helper_wider_labels
    page_title = 'Update Instructor Profile'

    if request.method == 'POST':
        form = ProfileUpdateRequestForm(request.POST)

        if form.is_valid():
            form.save()

            # TODO: email notification?

            context = {
                'title': 'Thank you for updating your instructor profile',
            }
            return render(request,
                          'forms/profileupdate_confirm.html',
                          context)
        else:
            messages.error(request, 'Fix errors below.')

    context = {
        'title': page_title,
        'form': form,
        'form_helper': form_helper,
    }
    return render(request, 'forms/profileupdate.html', context)


class AllProfileUpdateRequests(LoginRequiredMixin, ListView):
    active_requests = True
    context_object_name = 'requests'
    template_name = 'workshops/all_profileupdaterequests.html'

    def get_queryset(self):
        return ProfileUpdateRequest.objects \
                                   .filter(active=self.active_requests) \
                                   .order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'Instructor profile update requests'
        context['active_requests'] = self.active_requests
        return context


class AllClosedProfileUpdateRequests(AllProfileUpdateRequests):
    active_requests = False


@login_required
def profileupdaterequest_details(request, request_id):
    update_request = get_object_or_404(ProfileUpdateRequest,
                                       pk=request_id)

    person_selected = False

    # Nested lookup.
    # First check if there's person with the same email, then maybe check if
    # there's a person with the same first and last names.
    try:
        person = Person.objects.get(email=update_request.email)
        form = None
    except Person.DoesNotExist:
        try:
            person = Person.objects.get(personal=update_request.personal,
                                        family=update_request.family)
            form = None
        except (Person.DoesNotExist, Person.MultipleObjectsReturned):
            # Either none or multiple people with the same first and last
            # names.
            # But the user might have submitted some person by themselves. We
            # should check that!
            try:
                person = Person.objects.get(pk=int(request.GET['person_1']))
                person_selected = True
                form = PersonLookupForm(request.GET)
            except KeyError:
                person = None
                # if the form wasn't submitted, initialize it without any
                # input data
                form = PersonLookupForm()
            except (ValueError, Person.DoesNotExist):
                person = None

    if person:
        # check if the person has instructor badge
        person.has_instructor_badge = Award.objects.filter(
            badge__in=Badge.objects.instructor_badges(), person=person
        ).exists()

    try:
        airport = Airport.objects.get(iata=update_request.airport_iata)
    except Airport.DoesNotExist:
        airport = None

    context = {
        'title': ('Instructor profile update request #{}'
                  .format(update_request.pk)),
        'new': update_request,
        'old': person,
        'person_form': form,
        'person_selected': person_selected,
        'form_helper': bootstrap_helper_get,
        'airport': airport,
    }
    return render(request, 'workshops/profileupdaterequest.html', context)


class ProfileUpdateRequestFix(LoginRequiredMixin, PermissionRequiredMixin,
                              UpdateViewContext):
    perms = 'workshops.change_profileupdaterequest'
    model = ProfileUpdateRequest
    form_class = ProfileUpdateRequestFormNoCaptcha
    pk_url_kwarg = 'request_id'
    template_name = 'workshops/generic_form.html'


@login_required
@permission_required('workshops.change_profileupdaterequest',
                     raise_exception=True)
def profileupdaterequest_discard(request, request_id):
    """Discard ProfileUpdateRequest, ie. set it to inactive."""
    profileupdate = get_object_or_404(ProfileUpdateRequest, active=True,
                                      pk=request_id)
    profileupdate.active = False
    profileupdate.save()

    messages.success(request,
                     'Profile update request was discarded successfully.')
    return redirect(reverse('all_profileupdaterequests'))


@login_required
@permission_required(['workshops.change_profileupdaterequest',
                      'workshops.change_person'], raise_exception=True)
def profileupdaterequest_accept(request, request_id, person_id):
    """
    Accept the profile update by rewriting values to selected user's profile.

    IMPORTANT: we do not rewrite all of the data users input (like
    other gender, or other lessons).  All of it is still in
    the database model ProfileUpdateRequest, but does not get written to the
    Person model object.
    """
    profileupdate = get_object_or_404(ProfileUpdateRequest, active=True,
                                      pk=request_id)
    person = get_object_or_404(Person, pk=person_id)
    person_name = str(person)

    airport = get_object_or_404(Airport, iata=profileupdate.airport_iata)

    person.personal = profileupdate.personal
    person.family = profileupdate.family
    person.email = profileupdate.email
    person.affiliation = profileupdate.affiliation
    person.airport = airport
    person.github = profileupdate.github
    person.twitter = profileupdate.twitter
    person.url = profileupdate.website
    # if occupation is "Other", simply save the `occupation_other` field,
    # otherwise get full display of occupation (since it's a choice field)
    if profileupdate.occupation == '':
        person.occupation = profileupdate.occupation_other
    else:
        person.occupation = profileupdate.get_occupation_display()
    person.orcid = profileupdate.orcid
    person.gender = profileupdate.gender
    person.domains = list(profileupdate.domains.all())

    # Since Person.lessons uses a intermediate model Qualification, we ought to
    # operate on Qualification objects instead of using Person.lessons as a
    # list.

    # erase old lessons
    Qualification.objects.filter(person=person).delete()
    # add new
    Qualification.objects.bulk_create([
        Qualification(person=person, lesson=L)
        for L in profileupdate.lessons.all()
    ])

    person.save()

    profileupdate.active = False
    profileupdate.save()

    messages.success(request,
                     '{} was updated successfully.'.format(person_name))
    return redirect(person.get_absolute_url())

#------------------------------------------------------------


@login_required
@permission_required('workshops.add_todoitem', raise_exception=True)
def todos_add(request, event_ident):
    """Add a standard TodoItems for a specific event."""
    try:
        event = Event.get_by_ident(event_ident)
    except Event.DoesNotExist:
        raise Http404('Event matching query does not exist.')

    dt = datetime.datetime
    timedelta = datetime.timedelta

    initial = []
    base = dt.now()
    if event.start and event.end:
        extra = 9
    else:
        extra = 10
        initial = [
            {
                'title': 'Set date with host',
                'due': dt.now() + timedelta(days=30),
                'event': event,
            },
        ]

    TodoFormSet = modelformset_factory(TodoItem, form=SimpleTodoForm,
                                       extra=extra)

    formset = TodoFormSet(queryset=TodoItem.objects.none(), initial=initial + [
        {
            'title': 'Set up a workshop website',
            'due': base + timedelta(days=7),
            'event': event,
        },
        {
            'title': 'Find instructor #1',
            'due': base + timedelta(days=14),
            'event': event,
        },
        {
            'title': 'Find instructor #2',
            'due': base + timedelta(days=14),
            'event': event,
        },
        {
            'title': 'Follow up that instructors have booked travel',
            'due': base + timedelta(days=21),
            'event': event,
        },
        {
            'title': 'Set up pre-workshop survey',
            'due': event.start - timedelta(days=7) if event.start else '',
            'event': event,
        },
        {
            'title': 'Make sure instructors are set with materials',
            'due': event.start - timedelta(days=1) if event.start else '',
            'event': event,
        },
        {
            'title': 'Submit invoice',
            'due': event.end + timedelta(days=2) if event.end else '',
            'event': event,
        },
        {
            'title': 'Make sure instructors are reimbursed',
            'due': event.end + timedelta(days=7) if event.end else '',
            'event': event,
        },
        {
            'title': 'Get attendee list',
            'due': event.end + timedelta(days=7) if event.end else '',
            'event': event,
        },
    ])

    if request.method == 'POST':
        formset = TodoFormSet(request.POST)
        if formset.is_valid():
            formset.save()
            messages.success(request, 'Successfully added a bunch of TODOs.',
                             extra_tags='todos')
            return redirect(reverse(event_details, args=(event.get_ident(), )))
        else:
            messages.error(request, 'Fix errors below.')

    context = {
        'title': 'Add standard TODOs to the event',
        'formset': formset,
        'helper': bootstrap_helper_inline_formsets,
        'event': event,
    }
    return render(request, 'workshops/todos_add.html', context)


@login_required
@permission_required('workshops.change_todoitem', raise_exception=True)
def todo_mark_completed(request, todo_id):
    todo = get_object_or_404(TodoItem, pk=todo_id)

    todo.completed = True
    todo.save()

    return HttpResponse()


@login_required
@permission_required('workshops.change_todoitem', raise_exception=True)
def todo_mark_incompleted(request, todo_id):
    todo = get_object_or_404(TodoItem, pk=todo_id)

    todo.completed = False
    todo.save()

    return HttpResponse()


class TodoItemUpdate(LoginRequiredMixin, PermissionRequiredMixin,
                     UpdateViewContext):
    perms = 'workshops.change_todoitem'
    model = TodoItem
    form_class = SimpleTodoForm
    pk_url_kwarg = 'todo_id'
    template_name = 'workshops/generic_form.html'

    def get_success_url(self):
        return reverse('event_details', args=[self.object.event.get_ident()])

    def form_valid(self, form):
        """Overwrite default way of showing the success message, because we
        need to add extra tags to it)."""
        self.object = form.save()

        # Important: we need to use ModelFormMixin.form_valid() here!
        # But by doing so we omit SuccessMessageMixin completely, so we need to
        # simulate it.  The code below is almost identical to
        # SuccessMessageMixin.form_valid().
        response = super(ModelFormMixin, self).form_valid(form)
        success_message = self.get_success_message(form.cleaned_data)
        if success_message:
            messages.success(self.request, success_message, extra_tags='todos')
        return response


@login_required
@permission_required('workshops.delete_todoitem', raise_exception=True)
def todo_delete(request, todo_id):
    """Delete a TodoItem. This is used on the event details page."""
    todo = get_object_or_404(TodoItem, pk=todo_id)
    event_ident = todo.event.get_ident()
    todo.delete()

    messages.success(request, 'TODO was deleted successfully.',
                     extra_tags='todos')

    return redirect(event_details, event_ident)


def _assign(request, obj, person_id):
    """Set obj.assigned_to. This view helper works with both POST and GET
    requests:

    * POST: read person ID from POST's person_1
    * GET: read person_id from URL
    * both: if person_id is None then make event.assigned_to empty
    * otherwise assign matching person.

    This is not a view, but it's used in some."""
    try:
        if request.method == "POST":
            person_id = request.POST.get('person_1', None)

        if person_id is None:
            obj.assigned_to = None
        else:
            person = Person.objects.get(pk=person_id)
            obj.assigned_to = person

        obj.save()

    except Person.DoesNotExist:
        raise Http404("No person found matching the query.")
