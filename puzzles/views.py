import json
import logging
import os
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from url_normalize import url_normalize

from . import tag_utils
from .forms import StatusForm, MetaPuzzleForm, PuzzleForm, TagForm
from .models import *
from .puzzle_tag import PuzzleTag
from accounts.models import Puzzler
from answers.forms import AnswerForm
from answers.models import Answer
from answers.views import AnswerView
from google_api_lib.google_api_client import GoogleApiClient
from slack_lib.slack_client import SlackClient


logger = logging.getLogger(__name__)


@login_required(login_url='/accounts/login/')
def index(request, pk):
    return HttpResponseRedirect(request.META.get('HTTP_REFERER', '/hunts/{}'.format(pk)))


@require_POST
@login_required(login_url='/accounts/login/')
def update_status(request, pk):
    form = StatusForm(request.POST, instance=get_object_or_404(Puzzle, pk=pk))
    if not form.is_valid():
        return JsonResponse({'error': 'Invalid update puzzle status form'},
            status=400)
    form.save()
    return JsonResponse({})


def __sanitize_guess(guess):
    return re.sub('[^0-9A-Za-z]', '', guess).upper()


@require_POST
@login_required(login_url='/accounts/login/')
@transaction.atomic
def guess(request, pk):
    form = AnswerForm(request.POST)
    puzzle = get_object_or_404(Puzzle.objects.select_for_update(), pk=pk)

    status_code = 200
    if form.is_valid() and puzzle.status != Puzzle.SOLVED:
        answer_text = __sanitize_guess(form.cleaned_data["text"])
        # If answer has already been added to the queue
        answer, created = Answer.objects.get_or_create(text=answer_text, puzzle=puzzle)
        if created:
            puzzle.status = Puzzle.PENDING
            puzzle.save()
            AnswerView.update_slack_with_puzzle_status(answer, answer.status)
        else:
            return JsonResponse(
                {'error': '"%s" has already been submitted as a guess for puzzle "%s"' % (answer_text, puzzle.name)},
                status=400)
    else:
        return JsonResponse(
            {'error': 'Answer form was invalid or puzzle was already solved for puzzle "%s"' % puzzle.name},
            status=400)

    return JsonResponse({}, status=status_code)

if settings.DEBUG:
    guess = csrf_exempt(guess)


@require_POST
@csrf_exempt
@transaction.atomic
def slack_guess(request):
    print("request data: " + str(request.POST))
    slack_message = request.POST
    if slack_message.get('token') != os.environ.get("SLACK_VERIFICATION_TOKEN"):
        return HttpResponseForbidden()

    answer_text = __sanitize_guess(slack_message.get('text'))
    channel_id = slack_message.get('channel_id')
    puzzle = get_object_or_404(Puzzle.objects.select_for_update(), channel=channel_id)
    print("puzzle: " + str(puzzle))
    if puzzle.status == Puzzle.SOLVED:
        return HttpResponse("Puzzle is already solved!")

    answer, created = Answer.objects.get_or_create(puzzle=puzzle, text=answer_text)
    if not created:
        return HttpResponse("The answer " + answer_text + " has already been submitted.")
    puzzle.status = Puzzle.PENDING
    puzzle.save()

    AnswerView.update_slack_with_puzzle_status(answer, answer.status)

    return HttpResponse("Answer " + answer_text + " has been submitted!")


@require_POST
@csrf_exempt
@transaction.atomic
def slack_events(request):
    '''
    Handles Slack member_joined_channel and member_left_channel events
    to keep track of active users per puzzle.
    '''
    event = json.loads(request.body)
    token = event.get('token')
    # TODO(erwa): Move SLACK_VERIFICATION_TOKEN into settings.py
    if token != os.environ.get("SLACK_VERIFICATION_TOKEN"):
        return HttpResponseForbidden()

    # one time events API verification
    challenge = event.get('challenge')
    if challenge:
        return HttpResponse(challenge)

    event = event.get('event')
    print('Received Slack event:', event)

    email = SlackClient.getInstance().get_user_email(event.get('user'))
    try:
        user = Puzzler.objects.get(email=email)
    except Puzzler.DoesNotExist as e:
        print('User with email', email, 'not found. Exception:', e)
        return HttpResponse('User with email ' + email + ' not found.')

    event_type = event.get('type')
    puzzle = get_object_or_404(Puzzle.objects.select_for_update(), channel=event.get('channel'))

    # we ignore the case where puzzle is accidentally marked as solved
    # and user joins/leaves during this time
    if puzzle.status != Puzzle.SOLVED:
        if event_type == 'member_joined_channel':
            puzzle.active_users.add(user)
        elif event_type == 'member_left_channel':
            puzzle.active_users.remove(user)

    return HttpResponse('Processed ' + event_type + ' event')


@require_POST
@login_required(login_url='/accounts/login/')
def set_metas(request, pk):
    old_metas = None
    new_metas = None
    with transaction.atomic():
        puzzle = get_object_or_404(Puzzle.objects.select_for_update(), pk=pk)
        form = MetaPuzzleForm(request.POST, instance=puzzle)
        if not form.is_valid():
            return JsonResponse(
                {'error': 'Invalid meta puzzle form submission'}, status=400)

        old_metas = list(puzzle.metas.all())
        new_metas = form.cleaned_data["metas"]
        # Check if any additional meta would introduce a cycle. If so, then stop the whole transaction.
        for new_meta in new_metas:
            if new_meta not in puzzle.metas.all() and is_ancestor(puzzle, new_meta):
                messages.error(request,
                    "")
                return JsonResponse(
                    {'error': 'Transaction cancelled: unable to assign metapuzzle '
                              'since doing so would introduce a meta-cycle.'},
                    status=400)

        puzzle.metas.set(new_metas)

    if new_metas:
        for new_meta in new_metas:
            GoogleApiClient.update_meta_sheet_feeders(new_meta)
        for old_meta in old_metas:
            if old_meta not in new_metas:
                GoogleApiClient.update_meta_sheet_feeders(old_meta)

    return JsonResponse({})

@require_POST
@login_required(login_url='/accounts/login/')
def edit_puzzle(request, pk):
    form = PuzzleForm(request.POST, auto_id=False)
    if not form.is_valid():
        return JsonResponse({'error': 'Invalid edit puzzle form'}, status=400)

    new_name = form.cleaned_data["name"]
    new_url = url_normalize(form.cleaned_data["url"])
    new_is_meta = form.cleaned_data["is_meta"]

    metas = None
    with transaction.atomic():
        puzzle = get_object_or_404(Puzzle.objects.select_for_update(), pk=pk)
        try:
            puzzle.update_metadata(new_name, new_url, new_is_meta)
            # TODO(asdfryan): Consider also renaming the slack channel to match the
            # new puzzle name.
            metas = puzzle.metas.all()

        except (DuplicatePuzzleNameError, DuplicatePuzzleUrlError, InvalidMetaPuzzleError) as e:
           return JsonResponse({'error': str(e)}, status=400)

    if metas:
        for meta in metas:
            GoogleApiClient.update_meta_sheet_feeders(meta)

    return JsonResponse({})


@require_POST
@login_required(login_url='/accounts/login/')
def delete_puzzle(request, pk):
    metas = None
    with transaction.atomic():
        puzzle = get_object_or_404(Puzzle.objects.select_for_update(), pk=pk)
        if puzzle.is_meta and Puzzle.objects.filter(metas__id=pk):
            return JsonResponse(
                {'error': "Metapuzzles can only be deleted or made non-meta if no "
                          "other puzzles are assigned to it."}, status=400)
        else:
            metas = list(puzzle.metas.all())
            puzzle.delete()
            SlackClient.getInstance().archive_channel(puzzle.channel)

    if metas:
        for meta in metas:
            GoogleApiClient.update_meta_sheet_feeders(meta)

    return JsonResponse({})


@require_POST
@login_required(login_url='/accounts/login/')
def add_tag(request, pk):
    form = TagForm(request.POST)
    if not form.is_valid():
        return JsonResponse({'error': 'Invalid add tag form submission'}, status=400)

    metapuzzle = None
    with transaction.atomic():
        puzzle = get_object_or_404(Puzzle.objects.select_for_update(), pk=pk)
        (tag, _) = PuzzleTag.objects.update_or_create(
            name=form.cleaned_data["name"],
            defaults={'color' : form.cleaned_data["color"]}
        )
        if tag.is_meta:
            metapuzzle = get_object_or_404(Puzzle.objects.select_for_update(), name=tag.name)
            if is_ancestor(puzzle, metapuzzle):
                return JsonResponse({'error': '"Unable to assign metapuzzle since doing so would introduce a meta-cycle."'}, status=400)
            # the post m2m hook will add tag
            puzzle.metas.add(metapuzzle)
        else:
            puzzle.tags.add(tag)

    if metapuzzle:
        GoogleApiClient.update_meta_sheet_feeders(metapuzzle)

    return JsonResponse({})

if settings.DEBUG:
    add_tag = csrf_exempt(add_tag)


@require_POST
@login_required(login_url='/accounts/login/')
def remove_tag(request, pk, tag_text):
    meta = None
    with transaction.atomic():
        puzzle = get_object_or_404(Puzzle.objects.select_for_update(), pk=pk)
        if puzzle.name == tag_text:
            return JsonResponse({'error': "You cannot remove a meta's tag (%s) from itself" % tag_text}, status=400)
        try:
            tag = puzzle.tags.get(name=tag_text)
            if tag.is_meta:
                meta = Puzzle.objects.get(name=tag_text)
                # the post m2m hook will remove tag
                puzzle.metas.remove(meta)
            else:
                puzzle.tags.remove(tag_text)

            # clear db of dangling tags
            if not tag.tagged_items.exists():
                tag.delete()
        except ObjectDoesNotExist as e:
            return JsonResponse({'error': "Could not find the tag {} to remove".format(tag_text)}, status=400)

    if meta:
        GoogleApiClient.update_meta_sheet_feeders(meta)

    return JsonResponse({})


@login_required(login_url='/accounts/login/')
def add_tags_form(request, pk):
    puzzle = get_object_or_404(Puzzle, pk=pk)
    puzzle_tags = tag_utils.get_tags(puzzle)
    all_tags = tag_utils.get_all_tags()

    suggestions = [t for t in all_tags.items() if t not in puzzle_tags]
    suggestions.sort(key=lambda item: (PuzzleTag.color_ordering()[item[1]], item[0]))

    tag_form = TagForm()
    # For custom tags, we want to limit color choices to non-reserved colors.
    tag_form.fields['color'].choices = PuzzleTag.visible_color_choices()

    context = {
        'puzzle': puzzle,
        'suggestions': suggestions,
        'tag_form': tag_form,
    }
    html = render_to_string('modals/tags_form.html', context, request)
    return HttpResponse(html)


@login_required(login_url='/accounts/login/')
def meta_select_form(request, pk):
    puzzle = get_object_or_404(Puzzle, pk=pk)
    meta_form = MetaPuzzleForm(initial={'metas': puzzle.metas.all()}, instance=puzzle)
    return HttpResponse(meta_form.as_p())
