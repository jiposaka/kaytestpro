# -*- coding: utf-8 -*-
# testapp.urls
# 

# Following few lines is an example urlmapping with an older interface.
"""
from werkzeug.routing import EndpointPrefix, Rule

def make_rules():
  return [
    EndpointPrefix('testapp/', [
      Rule('/', endpoint='index'),
    ]),
  ]

all_views = {
  'testapp/index': 'testapp.views.index',
}
"""

from kay.routing import (
  ViewGroup, Rule
)

view_groups = [
  ViewGroup(
    Rule('/', endpoint='index', view='testapp.views.index'),
  )
]

