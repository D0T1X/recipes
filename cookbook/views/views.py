import copy
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash, authenticate
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django_tables2 import RequestConfig
from django.utils.translation import gettext as _

from django.conf import settings

from cookbook.filters import RecipeFilter
from cookbook.forms import *
from cookbook.helper.permission_helper import group_required
from cookbook.tables import RecipeTable, RecipeTableSmall, CookLogTable, ViewLogTable


def index(request):
    if not request.user.is_authenticated:
        if User.objects.count() < 1 and 'django.contrib.auth.backends.RemoteUserBackend' not in settings.AUTHENTICATION_BACKENDS:
            return HttpResponseRedirect(reverse_lazy('view_setup'))
        return HttpResponseRedirect(reverse_lazy('view_search'))
    try:
        page_map = {
            UserPreference.SEARCH: reverse_lazy('view_search'),
            UserPreference.PLAN: reverse_lazy('view_plan'),
            UserPreference.BOOKS: reverse_lazy('view_books'),
        }

        return HttpResponseRedirect(page_map.get(request.user.userpreference.default_page))
    except UserPreference.DoesNotExist:
        return HttpResponseRedirect(reverse_lazy('view_search'))


def search(request):
    if request.user.is_authenticated:
        f = RecipeFilter(request.GET, queryset=Recipe.objects.all().order_by('name'))

        if request.user.userpreference.search_style == UserPreference.LARGE:
            table = RecipeTable(f.qs)
        else:
            table = RecipeTableSmall(f.qs)
        RequestConfig(request, paginate={'per_page': 25}).configure(table)

        if request.GET == {} and request.user.userpreference.show_recent:
            qs = Recipe.objects.filter(viewlog__created_by=request.user).order_by('-viewlog__created_at').all()

            recent_list = []
            for r in qs:
                if r not in recent_list:
                    recent_list.append(r)
                if len(recent_list) >= 5:
                    break

            last_viewed = RecipeTable(recent_list)
        else:
            last_viewed = None

        return render(request, 'index.html', {'recipes': table, 'filter': f, 'last_viewed': last_viewed})
    else:
        return render(request, 'index.html')


@group_required('guest')
def recipe_view(request, pk):
    recipe = get_object_or_404(Recipe, pk=pk)
    ingredients = RecipeIngredient.objects.filter(recipe=recipe)
    comments = Comment.objects.filter(recipe=recipe)

    if request.method == "POST":
        comment_form = CommentForm(request.POST, prefix='comment')
        if comment_form.is_valid():
            comment = Comment()
            comment.recipe = recipe
            comment.text = comment_form.cleaned_data['text']
            comment.created_by = request.user

            comment.save()

            messages.add_message(request, messages.SUCCESS, _('Comment saved!'))

        bookmark_form = RecipeBookEntryForm(request.POST, prefix='bookmark')
        if bookmark_form.is_valid():
            bookmark = RecipeBookEntry()
            bookmark.recipe = recipe
            bookmark.book = bookmark_form.cleaned_data['book']

            bookmark.save()

            messages.add_message(request, messages.SUCCESS, _('Bookmark saved!'))

    comment_form = CommentForm()
    bookmark_form = RecipeBookEntryForm()

    if request.user.is_authenticated:
        if not ViewLog.objects.filter(recipe=recipe).filter(created_by=request.user).filter(created_at__gt=(timezone.now() - timezone.timedelta(minutes=5))).exists():
            ViewLog.objects.create(recipe=recipe, created_by=request.user)

    return render(request, 'recipe_view.html',
                  {'recipe': recipe, 'ingredients': ingredients, 'comments': comments, 'comment_form': comment_form,
                   'bookmark_form': bookmark_form})


@group_required('user')
def books(request):
    book_list = []

    books = RecipeBook.objects.filter(Q(created_by=request.user) | Q(shared=request.user)).distinct().all()

    for b in books:
        book_list.append({'book': b, 'recipes': RecipeBookEntry.objects.filter(book=b).all()})

    return render(request, 'books.html', {'book_list': book_list})


def get_start_end_from_week(p_year, p_week):
    first_day_of_week = datetime.strptime(f'{p_year}-W{int(p_week) - 1}-1', "%Y-W%W-%w").date()
    last_day_of_week = first_day_of_week + timedelta(days=6.9)
    return first_day_of_week, last_day_of_week


def get_days_from_week(start, end):
    delta = end - start
    days = []
    for i in range(delta.days + 1):
        days.append(start + timedelta(days=i))
    return days


@group_required('user')
def meal_plan(request):
    js_week = datetime.now().strftime("%Y-W%V")
    if request.method == "POST":
        js_week = request.POST['week']

    year, week = js_week.split('-')
    first_day, last_day = get_start_end_from_week(year, week.replace('W', ''))

    surrounding_weeks = {'next': (last_day + timedelta(3)).strftime("%Y-W%V"), 'prev': (first_day - timedelta(3)).strftime("%Y-W%V")}

    days = get_days_from_week(first_day, last_day)
    days_dict = {}
    for d in days:
        days_dict[d] = []

    plan = {}
    for t in MealPlan.MEAL_TYPES:
        plan[t[0]] = {'type_name': t[1], 'days': copy.deepcopy(days_dict)}

    for d in days:
        plan_day = MealPlan.objects.filter(date=d).filter(Q(created_by=request.user) | Q(shared=request.user)).distinct().all()
        for p in plan_day:
            plan[p.meal]['days'][d].append(p)

    return render(request, 'meal_plan.html', {'js_week': js_week, 'plan': plan, 'days': days, 'surrounding_weeks': surrounding_weeks})


@group_required('user')
def meal_plan_entry(request, pk):
    plan = MealPlan.objects.get(pk=pk)

    if plan.created_by != request.user and plan.shared != request.user:
        messages.add_message(request, messages.ERROR, _('You do not have the required permissions to view this page!'))
        return HttpResponseRedirect(reverse_lazy('index'))

    same_day_plan = MealPlan.objects.filter(date=plan.date).exclude(pk=plan.pk).filter(Q(created_by=request.user) | Q(shared=request.user)).order_by('meal').all()

    return render(request, 'meal_plan_entry.html', {'plan': plan, 'same_day_plan': same_day_plan})


@group_required('user')
def shopping_list(request):
    markdown_format = True

    if request.method == "POST":
        form = ShoppingForm(request.POST)
        if form.is_valid():
            recipes = form.cleaned_data['recipe']
            markdown_format = form.cleaned_data['markdown_format']
        else:
            recipes = []
    else:
        raw_list = request.GET.getlist('r')

        recipes = []
        for r in raw_list:
            if re.match(r'^([1-9])+$', r):
                if Recipe.objects.filter(pk=int(r)).exists():
                    recipes.append(int(r))

        markdown_format = False
        form = ShoppingForm(initial={'recipe': recipes, 'markdown_format': False})

    ingredients = []

    for r in recipes:
        for ri in RecipeIngredient.objects.filter(recipe=r).exclude(unit__name__contains='Special:').all():
            index = None
            for x, ig in enumerate(ingredients):
                if ri.ingredient == ig.ingredient and ri.unit == ig.unit:
                    index = x

            if index:
                ingredients[index].amount = ingredients[index].amount + ri.amount
            else:
                ingredients.append(ri)

    return render(request, 'shopping_list.html', {'ingredients': ingredients, 'recipes': recipes, 'form': form, 'markdown_format': markdown_format})


@group_required('guest')
def user_settings(request):
    up = request.user.userpreference

    user_name_form = UserNameForm(instance=request.user)
    password_form = PasswordChangeForm(request.user)

    if request.method == "POST":
        if 'preference_form' in request.POST:
            form = UserPreferenceForm(request.POST, prefix='preference')
            if form.is_valid():
                if not up:
                    up = UserPreference(user=request.user)
                up.theme = form.cleaned_data['theme']
                up.nav_color = form.cleaned_data['nav_color']
                up.default_unit = form.cleaned_data['default_unit']
                up.default_page = form.cleaned_data['default_page']
                up.show_recent = form.cleaned_data['show_recent']
                up.search_style = form.cleaned_data['search_style']
                up.plan_share.set(form.cleaned_data['plan_share'])
                up.save()

        if 'user_name_form' in request.POST:
            user_name_form = UserNameForm(request.POST, prefix='name')
            if user_name_form.is_valid():
                request.user.first_name = user_name_form.cleaned_data['first_name']
                request.user.last_name = user_name_form.cleaned_data['last_name']
                request.user.save()

        if 'password_form' in request.POST:
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)

    if up:
        preference_form = UserPreferenceForm(instance=up)
    else:
        preference_form = UserPreferenceForm()

    return render(request, 'settings.html', {'preference_form': preference_form, 'user_name_form': user_name_form, 'password_form': password_form})


@group_required('guest')
def history(request):
    view_log = ViewLogTable(ViewLog.objects.filter(created_by=request.user).order_by('-created_at').all())
    cook_log = CookLogTable(CookLog.objects.filter(created_by=request.user).order_by('-created_at').all())
    return render(request, 'history.html', {'view_log': view_log, 'cook_log': cook_log})


@group_required('admin')
def system(request):
    postgres = False if settings.DATABASES['default']['ENGINE'] == 'django.db.backends.postgresql_psycopg2' else True
    return render(request, 'system.html', {'gunicorn_media': settings.GUNICORN_MEDIA, 'debug': settings.DEBUG, 'postgres': postgres})


def setup(request):
    if User.objects.count() > 0 or 'django.contrib.auth.backends.RemoteUserBackend' in settings.AUTHENTICATION_BACKENDS:
        messages.add_message(request, messages.ERROR, _('The setup page can only be used to create the first user! If you have forgotten your superuser credentials please consult the django documentation on how to reset passwords.'))
        return HttpResponseRedirect(reverse('login'))

    if request.method == 'POST':
        form = SuperUserForm(request.POST)
        if form.is_valid():
            if form.cleaned_data['password'] != form.cleaned_data['password_confirm']:
                form.add_error('password', _('Passwords dont match!'))
            else:
                user = User(
                    username=form.cleaned_data['name'],
                    is_superuser=True
                )
                try:
                    validate_password(form.cleaned_data['password'], user=user)
                    user.set_password(form.cleaned_data['password'])
                    user.save()
                    messages.add_message(request, messages.SUCCESS, _('User has been created, please login!'))
                    return HttpResponseRedirect(reverse('login'))
                except ValidationError as e:
                    for m in e:
                        form.add_error('password', m)
    else:
        form = SuperUserForm()

    return render(request, 'setup.html', {'form': form})


def markdown_info(request):
    return render(request, 'markdown_info.html', {})
