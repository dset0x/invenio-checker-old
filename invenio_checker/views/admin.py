# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2012, 2013, 2014, 2015 CERN.
#
# Invenio is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""Checker Admin Flask Blueprint."""

from __future__ import unicode_literals
from importlib import import_module
from itertools import chain
from collections import defaultdict
from intbitset import intbitset

import six

from flask import (
    Blueprint,
    Response,
    redirect,
    request,
    stream_with_context,
    url_for,
    render_template_string,
    render_template,
)
from flask.json import jsonify
from flask_breadcrumbs import register_breadcrumb
from flask_login import login_required
from datetime import datetime

from wtforms import (  # pylint: disable=no-name-in-module
    Form,
    fields,
    validators,
    ValidationError,
)

from invenio.ext.sqlalchemy import db
from invenio.base.decorators import templated
from invenio.base.i18n import _
from invenio.ext.principal import permission_required
from ..acl import viewchecker, modifychecker

from ..models import (
    CheckerRule,
    CheckerRuleExecution,
    CheckerReporter,
    default_date,
)
from ..views.config import (
    task_mapping,
    check_mapping,
    log_mapping,
)

from ..registry import plugin_files, reporters_files

from ..recids import ids_from_input
import sys
from croniter import croniter
import json

from functools import partial
from invenio.utils import forms

blueprint = Blueprint('checker_admin', __name__,
                      url_prefix="/admin/checker",
                      template_folder='../templates',
                      static_folder='../static')

def get_NewTaskForm(*args, **kwargs):

    class NewTaskForm(Form):
        name = fields.StringField(
            'Task name',
            validators=[validators.InputRequired()],
        )
        plugin = fields.SelectField(
            'Check',
            choices=[(plugin, plugin) for plugin in plugin_files],
            validators=[validators.InputRequired()],
        )
        reporters = fields.SelectMultipleField(
            'Reporters',
            # XXX If you rename `reporter` to `plugin`, all hell breaks loose.
            choices=[(reporter, reporter) for reporter in reporters_files],
        )
        consider_deleted_records = fields.BooleanField(
            'Consider deleted records',
        )
        force_run_on_unmodified_records = fields.BooleanField(
            'Force run on unmodified records',
        )
        filter_pattern = fields.StringField(
            'Search pattern',
        )
        filter_records = fields.StringField(
            'Record IDs',
        )
        schedule = fields.StringField(
            'Schedule',
        )
        requested_action = fields.SelectField(
            'Requested action',
            choices=[
                ('submit_save',) * 2,
                ('submit_run_and_schedule',) * 2,
                ('submit_schedule',) * 2,
                ('submit_run',) * 2,
            ]
        )
        # Hidden
        schedule_enabled = fields.BooleanField(
            'Run this rule periodically',
        )
        modify = fields.BooleanField(
            'Request modification instead of creation',
        )
        original_name = fields.StringField(
            'Original name for modification',
        )

        def validate_filter_records(self, field):
            """Ensure that `filter_records` can be parsed by intbitset."""
            if not field.data:
                field.data = intbitset(trailing_bits=True)
            else:
                try:
                    field.data = ids_from_input(field.data)
                except TypeError:
                    etype, evalue, etb = sys.exc_info()
                    six.reraise(ValidationError, evalue, etb)

        def validate_schedule(self, field):
            """Ensure that `schedule` is accepted by `croniter`."""
            if not field.data:
                return
            try:
                croniter(field.data)
            except Exception:
                # May be TypeError/KeyError/AttributeError, who knows what else
                # Let's play it safe.
                six.reraise(ValidationError, *sys.exc_info()[1:])

    return NewTaskForm(*args, **kwargs)


@blueprint.route('/')
def index():
    """Redirect to the tasks view."""
    return redirect(url_for('.view', page_name='tasks'))


@blueprint.route('/api/records/get')
@login_required
@permission_required(viewchecker.name)
def record_brief():
    from invenio_search.api import Query
    records = Query(request.args['query']).search().records()[:5]
    return ''.join(render_template('format/record/Default_HTML_brief.tpl', record=i)
                   for i in records)


@blueprint.route('/view/<page_name>')
@login_required
@permission_required(viewchecker.name)
@templated('checker/admin/index.html')
@register_breadcrumb(blueprint, 'admin.checker_admin', _('Checker'))
def view(page_name):
    """Have javascript load the correct page."""
    return {
        'page_name': page_name,
        'new_task_form': get_NewTaskForm()
    }

# Tasks
@blueprint.route('/api/tasks/get/data', methods=['GET'])
@login_required
@permission_required(viewchecker.name)
def get_all_tasks_data():
    """
    Return a JSON representation of the CheckerRule data.

    For security reasons, a list cannot be JSON-ified, so it has to be wrapped
    within a dictionary.
    """
    column_list = {}
    for mapping_key, mapping_value in task_mapping.items():
        column_list[mapping_key] = mapping_value

    row_list = []
    rules = CheckerRule.query.all()
    for rule in rules:
        row_list.append(get_task_data(rule))
    return jsonify({'rows': row_list, 'cols': column_list})

@blueprint.route('/api/tasks/get/data/<task_name>', methods=['POST'])
@login_required
@permission_required(viewchecker.name)
def get_single_task(task_name):
    return jsonify(get_task_data(CheckerRule.query.get(task_name)))

def get_task_data(rule):
    rule_d = rule.__dict__  # Work around inveniosoftware/invenio-ext#16
    rule_d = {key: val for key, val in rule_d.items()
              if not key.startswith('_')}

    # Serialization hacks
    rule_d['arguments'] = json.dumps(rule_d['arguments'],
                                     default=default_date)
    if rule_d['filter_records'].is_infinite():
        rule_d['filter_records'] = ''
    else:
        rule_d['filter_records'] = ranges_str(rule_d['filter_records'])
    rule_d['reporters'] = [rep.plugin for rep in rule.reporters]
    return rule_d

# Checks
@blueprint.route('/api/checks/get/data', methods=['GET'])
@login_required
@permission_required(viewchecker.name)
def get_checks_data():
    """Returns the columns of the checks table."""
    checks = []
    for name, plugin in plugin_files.items():
        checks.append({
            'name': name,
            'description': plugin.__doc__,
        })
    return jsonify({'rows': checks, 'cols': check_mapping})


@blueprint.route('/api/checks/stream_check/<pluginspec>', methods=['GET'])
@login_required
@permission_required(viewchecker.name)
def stream_check(pluginspec):
    """
    TODO
    """
    def read_file():
        filepath = plugin_files[pluginspec].__file__
        with open(filepath, 'r') as file_:
            for line in file_:
                yield line
    return Response(stream_with_context(read_file()))


# executions
@blueprint.route('/api/executions/get/data', methods=['GET'])
@login_required
@permission_required(viewchecker.name)
def get_logs_data():
    """Returns the columns of the checks table."""
    loglist = []
    for execution in CheckerRuleExecution.query.all():
        loglist.append({
            'task':
            execution.rule.name,

            'start_date':
            int(execution.start_date.strftime("%s")) * 1000
            if execution.start_date else None,

            'status_update_date':
            int(execution.status_update_date.strftime("%s")) * 1000
            if execution.status_update_date else None,

            'status':
            str(execution.status.name),

            'owner':
            execution.owner.nickname
            if execution.owner else '',

            'owner_id':
            execution.owner.id
            if execution.owner else -1,

            'uuid':
            execution.uuid,
        })
    return jsonify({"rows": loglist, "cols": log_mapping})


@blueprint.route('/api/executions/stream_structured/<uuid>', methods=['GET'])
@login_required
@permission_required(viewchecker.name)
def stream_logs(uuid):
    """Returns the columns of the checks table."""
    execution = CheckerRuleExecution.query.get(uuid)
    return Response(stream_with_context(execution.read_logs()))

@blueprint.route('/api/task_create/get_arguments_spec', methods=['POST'])
@login_required
@permission_required(viewchecker.name)
def task_create():
    """Return complementary form fields for a check's arguments."""
    plugin_name = request.form['plugin_name']
    form = get_ArgForm(plugin_name)
    task_name = request.form.get('task_name')

    # If a `task_name` was sent in the request, then an already-existing's
    # plugin's contents should be filled-in. (eg editing an existing task)
    if task_name:
        if plugin_name in plugin_files:
            task = CheckerRule.query.get(task_name)
        elif plugin_name in reporters_files:
            task = CheckerReporter.query.filter(
                CheckerReporter.rule_name == task_name,
                CheckerReporter.plugin == plugin_name).first()
        # The query will only have returned something if the specified
        # task_name already has a relationship with a `plugin_name` type
        # reporter.
        if task:
            for key, val in task.arguments.items():
                key = "arg_"+plugin_name+"_"+key
                field = getattr(form, key)
                # For choice fields, the field requires that the `data` is
                # `str(idx_of_selection)`
                if hasattr(field, 'choices'):
                    if isinstance(val, six.text_type): # Single choice
                        val = next((str(idx) for idx, choice in field.choices
                                    if choice == val))
                    else:  # Multi choice
                        # UNTESTED XXX
                        val = [str(idx) for idx, choice in field.choices
                               if choice in val]
                setattr(field, 'data', val)

    return render_template_string('''
    {% import 'checker/admin/macros_bootstrap.html' as bt %}
    {{ bt.render_form(form, nested=True) }}
    ''', form=form)

def get_ArgForm(plugin_name, *args, **kwargs):
    """Get a WTForms form based on the cerberus schema defined in the check."""

    class ArgForm(Form):
        """Empty form to populate based on plugin-provided schema."""

        @property
        def data_for_db(self):
            ret = {}
            for key, val in self.data.items():
                stripped_key = key[len(ArgForm.prefix+"_"):]
                field = getattr(self, key)
                schema = ArgForm.schema
                # Don't change the order of the checks here:
                # SelectMultipleField inherits from SelectField
                if isinstance(field, fields.SelectMultipleField):
                    val = [schema[stripped_key]['values'][int(itm)]
                           for itm in val]
                elif isinstance(field, fields.SelectField):
                    val = schema[stripped_key]['values'][int(val)]
                ret[stripped_key] = val
            return ret

    type_to_wtforms = {
        "string": partial(fields.StringField, validators=[validators.InputRequired()]),
        "text": partial(fields.TextAreaField, validators=[validators.InputRequired()]),
        "integer": partial(fields.IntegerField, validators=[validators.InputRequired()]),
        "float": partial(fields.DecimalField, validators=[validators.InputRequired()]),
        "decimal": partial(fields.DecimalField, validators=[validators.InputRequired()]),
        "boolean": partial(fields.BooleanField, validators=[]),

        "choice": partial(fields.SelectField, validators=[validators.InputRequired()]),
        "choice_multi": partial(fields.SelectMultipleField, validators=[]),

        "datetime": partial(fields.DateTimeField, widget=forms.DateTimePickerWidget()),
        "date": partial(fields.DateField, widget=forms.DatePickerWidget()),
    }

    def get_schema(plugin_name):
        if plugin_name not in plugin_files.keys() + reporters_files.keys():
            raise Exception(plugin_name)  # TODO
        module = import_module(plugin_name)
        try:
            return module.argument_schema
        except AttributeError:
            return {}

    def build_choices(schema, type_name, key):
        if type_name in ('choice', 'choice_multi'):
            ret = {'choices':[]}
            for idx, choice in enumerate(schema[key]['values']):
                ret['choices'].append((idx, choice))
            return ret
        else:
            return {}

    # FIXME: These names might create collisions?
    ArgForm.plugin_name = plugin_name
    ArgForm.prefix = "arg_{}".format(plugin_name)
    ArgForm.schema = get_schema(plugin_name)
    for key, spec in ArgForm.schema.items():
        type_name = spec['type']
        type_kwargs = build_choices(ArgForm.schema, type_name, key)
        try:
            type_kwargs['label'] = spec['label']
        except KeyError:
            type_kwargs['label'] = key
        prefixed_key = "{}_{}".format(ArgForm.prefix, key)
        setattr(ArgForm, prefixed_key, type_to_wtforms[type_name](**type_kwargs))

    return ArgForm(*args, **kwargs)


@blueprint.route('/api/task_create/submit', methods=['POST'])
@login_required
@permission_required(viewchecker.name)
def submit_task():
    """Record a user-created task to the database and run."""
    from ..supervisor import run_task

    def failure(type_, errors):
        assert type_ in ('general', 'validation')
        return jsonify({'failure_type': type_, 'errors': errors}), 400

    def success():
        return jsonify({})

    # Recreate the forms that we have previously served to the user so that we
    # can validate.
    form_origin = get_NewTaskForm(request.form)
    form_plugin = get_ArgForm(request.form['plugin'], request.form)
    if not (form_origin.validate() & form_plugin.validate()):
        form_errors = defaultdict(list)
        for field, errors in chain(form_origin.errors.items(),
                                   form_plugin.errors.items()):
            form_errors[field].extend(errors)
        return failure('validation', form_errors)

    # Get a dictionary that we can pass as kwargs to the database object,
    form_for_db = dict(form_origin.data)
    # but first, pop metadata out of it.
    form_for_db.pop('modify', False)
    modify = 'modify' in request.form
    original_name = form_for_db.pop('original_name', False)
    requested_action = form_for_db.pop('requested_action', False)
    reporter_names = form_for_db.pop('reporters', [])

    if modify:
        task = CheckerRule.query.get(original_name)
        for key, val in form_for_db.iteritems():
            setattr(task, key, val)
    else:
        task = CheckerRule(**form_for_db)

    # Modify the rule as requested. Don't flush until we say so so that any
    # exceptions are in the try block.
    with db.session.no_autoflush:
        # Add arguments
        task.arguments = form_plugin.data_for_db
        # Add reporters
        form_reporters = [
            get_ArgForm(reporter_name, request.form)
            for reporter_name in reporter_names
        ]
        new_reporters = []
        if form_reporters:
            for form_reporter in form_reporters:
                new = CheckerReporter(
                    plugin=form_reporter.plugin_name,
                    arguments=form_reporter.data_for_db,
                    rule_name=task.name,
                )
                new_reporters.append(new)
                db.session.add(new)
        task.reporters = new_reporters

    try:
        db.session.add(task)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return failure('general', (str(e),))

    if requested_action.startswith('submit_run'):
        try:
            run_task(task.name)
        except Exception as e:
            return failure('general', (str(e),))

    return success()

@blueprint.route('/translate', methods=['GET'])
@login_required
@permission_required(viewchecker.name)
def translate():
    """Returns the columns of the checks table."""
    from invenio.base.i18n import _
    return str(_(request.args['english']))

@blueprint.route('/task_run', methods=['GET'])
@login_required
@permission_required(modifychecker.name)
def task_run():
    from ..manage import start_rules
    names_in_db = CheckerRule.query.with_entities(CheckerRule.name).all()
    names_in_db = {tup[0] for tup in names_in_db}
    task_names = request.values.getlist('task_names[]')
    if set(task_names) - names_in_db:
        return jsonify({'error': 'Missing tasks requested'}), 400
    start_rules(*task_names)
    return jsonify({})

@blueprint.route('/task_delete', methods=['GET'])
@login_required
@permission_required(modifychecker.name)
def task_delete():
    from ..manage import delete_rule
    task_names = request.values.getlist('task_names[]')
    for task_name in task_names:
        delete_rule(task_name)
    return jsonify({})

# @blueprint.route('/task_modify/<plugin_name>', methods=['GET'])
# @login_required
# @permission_required(modifychecker.name)
# def task_modify(plugin_name):
#     form = get_ArgForm(plugin_name)
#     return render_template_string('''
#     {% import 'checker/admin/macros_bootstrap.html' as bt %}
#     {{ bt.render_form(form, nested=True) }}
#     ''', form=form)

def get_ranges(items):
    from operator import itemgetter
    from itertools import groupby
    for k, g in groupby(enumerate(sorted(set(items))), lambda (i,x):i-x):
           yield map(itemgetter(1), g)

def ranges_str(items):
    output = ""
    for cont_set in get_ranges(items):
        if len(cont_set) == 1:
            output = output + "," + str(cont_set[0])
        else:
            output = output + "," + str(cont_set[0]) + "-" + str(cont_set[-1])
    output = output.lstrip(",")
    return output
