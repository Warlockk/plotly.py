import collections
import re
import typing as typ
import warnings
from contextlib import contextmanager
from copy import deepcopy, copy
from pprint import PrettyPrinter
from typing import Dict, Tuple, Union, Callable, List

from plotly.optional_imports import get_module

import plotly.offline as pyo
from _plotly_utils.basevalidators import (
    CompoundValidator, CompoundArrayValidator, BaseDataValidator,
    BaseValidator)
from plotly import animation
from plotly.callbacks import (Points, BoxSelector, LassoSelector,
                              InputDeviceState)
from plotly.utils import ElidedPrettyPrinter
from plotly.validators import (DataValidator, LayoutValidator, FramesValidator)

# Optional imports
# ----------------
np = get_module('numpy')

# Create Undefined sentinel value
#   - Setting a property to None removes any existing value
#   - Setting a property to Undefined leaves existing value unmodified
Undefined = object()


class BaseFigure:
    """
    Base class for all figure types (both widget and non-widget)
    """

    # Constructor
    # -----------
    def __init__(self, data=None, layout_plotly=None, frames=None):
        """
        Construct a BaseFigure object

        Parameters
        ----------
        data
            One of:
            - A list or tuple of trace objects (or dicts that can be coerced
            into trace objects)

            - If `data` is a dict that contains a 'data',
            'layout', or 'frames' key then these values are used to
            construct the figure.

            - If `data` is a `BaseFigure` instance then the `data`, `layout`,
            and `frames` properties are extracted from the input figure
        layout_plotly
            The plotly layout dict.

            Note: this property is named `layout_plotly` rather than `layout`
            to deconflict it with the `layout` constructor parameter of the
            `widgets.DOMWidget` ipywidgets class, as the `BaseFigureWidget`
            class is a subclass of both BaseFigure and widgets.DOMWidget.

            If the `data` property is a BaseFigure instance, or a dict that
            contains a 'layout' key, then this property is ignored.
        frames
            A list or tuple of `plotly.graph_objs.Frame` objects (or dicts
            that can be coerced into Frame objects)

            If the `data` property is a BaseFigure instance, or a dict that
            contains a 'frames' key, then this property is ignored.
        """
        super().__init__()

        # Assign layout_plotly to layout
        # ------------------------------
        # See docstring note for explanation
        layout = layout_plotly

        # Subplot properties
        # ------------------
        # These properties are used by the tools.make_subplots logic.
        # We initialize them to None here, before checking if the input data
        # object is a BaseFigure, in which case we bring over the _grid*
        # properties of the input BaseFigure
        self._grid_str = None
        self._grid_ref = None

        # Handle case where data is a Figure or Figure-like dict
        # ------------------------------------------------------
        if isinstance(data, BaseFigure):
            # Bring over subplot fields
            self._grid_str = data._grid_str
            self._grid_ref = data._grid_ref

            # Extract data, layout, and frames
            data, layout, frames = data.data, data.layout, data.frames

        elif (isinstance(data, dict)
              and ('data' in data or 'layout' in data or 'frames' in data)):
            data, layout, frames = (data.get('data', None),
                                    data.get('layout', None),
                                    data.get('frames', None))

        # Handle data (traces)
        # --------------------
        # ### Construct data validator ###
        # This is the validator that handles importing sequences of trace
        # objects
        self._data_validator = DataValidator()

        # ### Import traces ###
        data = self._data_validator.validate_coerce(data)

        # ### Save tuple of trace objects ###
        self._data_objs = data

        # ### Import clone of trace properties ###
        # The _data property is a list of dicts containing the properties
        # explicitly set by the user for each trace.
        self._data = [deepcopy(trace._props) for trace in data]

        # ### Create data defaults ###
        # _data_defaults is a tuple of dicts, one for each trace. When
        # running in a widget context, these defaults are populated with
        # all property values chosen by the Plotly.js library that
        # aren't explicitly specified by the user.
        #
        # Note: No property should exist in both the _data and
        # _data_defaults for the same trace.
        self._data_defaults = [{} for _ in data]

        # ### Reparent trace objects ###
        for trace in data:
            # By setting the trace's parent to be this figure, we tell the
            # trace object to use the figure's _data and _data_defaults
            # dicts to get/set it's properties, rather than using the trace
            # object's internal _orphan_props dict.
            trace._parent = self

            # We clear the orphan props since the trace no longer needs then
            trace._orphan_props.clear()

        # Layout
        # ------
        # ### Construct layout validator ###
        # This is the validator that handles importing Layout objects
        self._layout_validator = LayoutValidator()

        # ### Import Layout ###
        self._layout_obj = self._layout_validator.validate_coerce(layout)

        # ### Import clone of layout properties ###
        self._layout = deepcopy(self._layout_obj._props)

        # ### Initialize layout defaults dict ###
        self._layout_defaults = {}

        # ### Reparent layout object ###
        self._layout_obj._orphan_props.clear()
        self._layout_obj._parent = self

        # Frames
        # ------

        # ### Construct frames validator ###
        # This is the validator that handles importing sequences of frame
        # objects
        self._frames_validator = FramesValidator()

        # ### Import frames ###
        self._frame_objs = self._frames_validator.validate_coerce(frames)

        # Note: Because frames are not currently supported in the widget
        # context, we don't need to follow the pattern above and create
        # _frames and _frame_defaults properties and then reparent the
        # frames. The figure doesn't need to be notified of
        # changes to the properties in the frames object hierarchy.

        # Context manager
        # ---------------

        # ### batch mode indicator ###
        # Flag that indicates whether we're currently inside a batch_*()
        # context
        self._in_batch_mode = False

        # ### Batch trace edits ###
        # Dict from trace indexes to trace edit dicts. These trace edit dicts
        # are suitable as `data` elements of Plotly.animate, but not
        # the Plotly.update (See `_build_update_params_from_batch`)
        #
        # type: typ.Dict[int, typ.Dict[str, typ.Any]]
        self._batch_trace_edits = {}

        # ### Batch layout edits ###
        # Dict from layout properties to new layout values. This dict is
        # directly suitable for use in Plotly.animate and Plotly.update
        # type: typ.Dict[str, typ.Any]
        self._batch_layout_edits = {}

        # Animation property validators
        # -----------------------------
        self._animation_duration_validator = animation.DurationValidator()
        self._animation_easing_validator = animation.EasingValidator()

    # Magic Methods
    # -------------
    def __setitem__(self, prop, value):

        # Normalize prop
        # --------------
        # Convert into a property tuple
        orig_prop = prop
        prop = BaseFigure._str_to_dict_path(prop)

        # Handle empty case
        # -----------------
        if len(prop) == 0:
            raise KeyError(orig_prop)

        # Handle scalar case
        # ------------------
        # e.g. ('foo',)
        elif len(prop) == 1:
            # ### Unwrap scalar tuple ###
            prop = prop[0]

            if prop == 'data':
                self.data = value
            elif prop == 'layout':
                self.layout = value
            elif prop == 'frames':
                self.frames = value
            else:
                raise KeyError(prop)

        # Handle non-scalar case
        # ----------------------
        # e.g. ('foo', 1)
        else:
            res = self
            for p in prop[:-1]:
                res = res[p]

            res[prop[-1]] = value

    def __setattr__(self, prop, value):
        """
        Parameters
        ----------
        prop : str
            The name of a direct child of this object
        value
            New property value
        Returns
        -------
        None
        """
        if prop.startswith('_') or hasattr(self, prop):
            # Let known properties and private properties through
            super().__setattr__(prop, value)
        else:
            # Raise error on unknown public properties
            raise AttributeError(prop)

    def __getitem__(self, prop):

        # Normalize prop
        # --------------
        # Convert into a property tuple
        orig_prop = prop
        prop = BaseFigure._str_to_dict_path(prop)

        # Handle scalar case
        # ------------------
        # e.g. ('foo',)
        if len(prop) == 1:
            # Unwrap scalar tuple
            prop = prop[0]

            if prop == 'data':
                return self._data_validator.present(self._data_objs)
            elif prop == 'layout':
                return self._layout_validator.present(self._layout_obj)
            elif prop == 'frames':
                return self._frames_validator.present(self._frame_objs)
            else:
                raise KeyError(orig_prop)

        # Handle non-scalar case
        # ----------------------
        # e.g. ('foo', 1)
        else:
            res = self
            for p in prop:
                res = res[p]

            return res

    def __iter__(self):
        return iter(('data', 'layout', 'frames'))

    def __contains__(self, prop):
        return prop in ('data', 'layout', 'frames')

    def __eq__(self, other):
        if not isinstance(other, BaseFigure):
            # Require objects to both be BaseFigure instances
            return False
        else:
            # Compare plotly_json representations

            # Use _vals_equal instead of `==` to handle cases where
            # underlying dicts contain numpy arrays
            return BasePlotlyType._vals_equal(self.to_plotly_json(),
                                              other.to_plotly_json())

    def __repr__(self):
        """
        Customize Figure representation when displayed in the
        terminal/notebook
        """
        repr_str = BasePlotlyType._build_repr_for_class(
            props=self.to_plotly_json(),
            class_name=self.__class__.__name__)

        return repr_str

    def update(self, dict1=None, **kwargs):
        """
        Update the properties of the figure with a dict and/or with
        keyword arguments.

        This recursively updates the structure of the figure
        object with the values in the input dict / keyword arguments.

        Parameters
        ----------
        dict1 : dict
            Dictionary of properties to be updated
        kwargs :
            Keyword/value pair of properties to be updated

        Examples
        --------
        >>> import plotly.graph_objs as go
        >>> fig = go.Figure(data=[{'y': [1, 2, 3]}])
        >>> fig.update(data=[{'y': [4, 5, 6]}])
        >>> fig.to_plotly_json()
            {'data': [{'type': 'scatter',
               'uid': 'e86a7c7a-346a-11e8-8aa8-a0999b0c017b',
               'y': array([4, 5, 6], dtype=int32)}],
             'layout': {}}

        >>> fig = go.Figure(layout={'xaxis':
        ...                         {'color': 'green',
        ...                          'range': [0, 1]}})
        >>> fig.update({'layout': {'xaxis': {'color': 'pink'}}})
        >>> fig.to_plotly_json()
            {'data': [],
             'layout': {'xaxis':
                        {'color': 'pink',
                         'range': [0, 1]}}}

        Returns
        -------
        BaseFigure
            Updated figure
        """
        with self.batch_update():
            for d in [dict1, kwargs]:
                if d:
                    for k, v in d.items():
                        BaseFigure._perform_update(self[k], v)

        return self

    # Data
    # ----
    @property
    def data(self):
        """
        The `data` property is a tuple of the figure's trace objects

        Returns
        -------
        tuple[BaseTraceType]
        """
        return self['data']

    @data.setter
    def data(self, new_data):

        # Validate new_data
        # -----------------
        err_header = ('The data property of a figure may only be assigned '
                      'a list or tuple that contains a permutation of a '
                      'subset of itself\n')

        # ### Check valid input type ###
        if not isinstance(new_data, (list, tuple)):
            err_msg = (err_header + '    Received value with type {typ}'
                       .format(typ=type(new_data)))
            raise ValueError(err_msg)

        # ### Check valid element types ###
        for trace in new_data:
            if not isinstance(trace, BaseTraceType):
                err_msg = (
                    err_header + '    Received element value of type {typ}'
                    .format(typ=type(trace)))
                raise ValueError(err_msg)

        # ### Check UIDs ###
        # Require that no new uids are introduced
        orig_uids = [_trace['uid'] for _trace in self._data]
        new_uids = [trace.uid for trace in new_data]

        invalid_uids = set(new_uids).difference(set(orig_uids))
        if invalid_uids:
            err_msg = (
                err_header + '    Invalid trace(s) with uid(s): {invalid_uids}'
                .format(invalid_uids=invalid_uids))

            raise ValueError(err_msg)

        # ### Check for duplicates in assignment ###
        uid_counter = collections.Counter(new_uids)
        duplicate_uids = [
            uid for uid, count in uid_counter.items() if count > 1
        ]
        if duplicate_uids:
            err_msg = (
                err_header + '    Received duplicated traces with uid(s): ' +
                '{duplicate_uids}'.format(duplicate_uids=duplicate_uids))

            raise ValueError(err_msg)

        # Remove traces
        # -------------
        remove_uids = set(orig_uids).difference(set(new_uids))
        delete_inds = []

        # ### Unparent removed traces ###
        for i, _trace in enumerate(self._data):
            if _trace['uid'] in remove_uids:
                delete_inds.append(i)

                # Unparent trace object to be removed
                old_trace = self.data[i]
                old_trace._orphan_props.update(deepcopy(old_trace._props))
                old_trace._parent = None

        # ### Compute trace props / defaults after removal ###
        traces_props_post_removal = [t for t in self._data]
        traces_prop_defaults_post_removal = [t for t in self._data_defaults]
        uids_post_removal = [trace_data['uid'] for trace_data in self._data]

        for i in reversed(delete_inds):
            del traces_props_post_removal[i]
            del traces_prop_defaults_post_removal[i]
            del uids_post_removal[i]

            # Modify in-place so we don't trigger serialization
            del self._data[i]

        if delete_inds:
            # Update widget, if any
            self._send_deleteTraces_msg(delete_inds)

        # Move traces
        # -----------

        # ### Compute new index for each remaining trace ###
        new_inds = []
        for uid in uids_post_removal:
            new_inds.append(new_uids.index(uid))

        # ### Compute current index for each remaining trace ###
        current_inds = list(range(len(traces_props_post_removal)))

        # ### Check whether a move is needed ###
        if not all([i1 == i2 for i1, i2 in zip(new_inds, current_inds)]):

            # #### Update widget, if any ####
            self._send_moveTraces_msg(current_inds, new_inds)

            # #### Reorder trace elements ####
            # We do so in-place so we don't trigger traitlet property
            # serialization for the FigureWidget case
            # ##### Remove by curr_inds in reverse order #####
            moving_traces_data = []
            for ci in reversed(current_inds):
                # Push moving traces data to front of list
                moving_traces_data.insert(0, self._data[ci])
                del self._data[ci]

            # #### Sort new_inds and moving_traces_data by new_inds ####
            new_inds, moving_traces_data = zip(
                *sorted(zip(new_inds, moving_traces_data)))

            # #### Insert by new_inds in forward order ####
            for ni, trace_data in zip(new_inds, moving_traces_data):
                self._data.insert(ni, trace_data)

        # ### Update data defaults ###
        # There is to front-end syncronization to worry about so this
        # operations doesn't need to be in-place
        self._data_defaults = [
            _trace for i, _trace in sorted(
                zip(new_inds, traces_prop_defaults_post_removal))
        ]

        # Update trace objects tuple
        self._data_objs = list(new_data)

    # Restyle
    # -------
    def plotly_restyle(self, restyle_data, trace_indexes=None, **kwargs):
        """
        Perform a Plotly restyle operation on the figure's traces

        Parameters
        ----------
        restyle_data : dict
            Dict of trace style updates.

            Keys are strings that specify the properties to be updated.
            Nested properties are expressed by joining successive keys on
            '.' characters (e.g. 'marker.color').

            Values may be scalars or lists. When values are scalars,
            that scalar value is applied to all traces specified by the
            `trace_indexes` parameter.  When values are lists,
            the restyle operation will cycle through the elements
            of the list as it cycles through the traces specified by the
            `trace_indexes` parameter.

            Caution: To use plotly_restyle to update a list property (e.g.
            the `x` property of the scatter trace), the property value
            should be a scalar list containing the list to update with. For
            example, the following command would be used to update the 'x'
            property of the first trace to the list [1, 2, 3]

            >>> fig.plotly_restyle({'x': [[1, 2, 3]]}, 0)

        trace_indexes : int or list of int
            Trace index, or list of trace indexes, that the restyle operation
            applies to. Defaults to all trace indexes.

        Returns
        -------
        None
        """

        # Normalize trace indexes
        # -----------------------
        trace_indexes = self._normalize_trace_indexes(trace_indexes)

        # Handle source_view_id
        # ---------------------
        # If not None, the source_view_id is the UID of the frontend
        # Plotly.js view that initially triggered this restyle operation
        # (e.g. the user clicked on the legend to hide a trace). We pass
        # this UID along so that the frontend views can determine whether
        # they need to apply the restyle operation on themselves.
        source_view_id = kwargs.get('source_view_id', None)

        # Perform restyle on trace dicts
        # ------------------------------
        restyle_changes = self._perform_plotly_restyle(restyle_data,
                                                       trace_indexes)
        if restyle_changes:
            # The restyle operation resulted in a change to some trace
            # properties, so we dispatch change callbacks and send the
            # restyle message to the frontend (if any)
            msg_kwargs = ({'source_view_id': source_view_id}
                          if source_view_id is not None
                          else {})

            self._send_restyle_msg(
                restyle_changes,
                trace_indexes=trace_indexes,
                **msg_kwargs)

            self._dispatch_trace_change_callbacks(
                restyle_changes, trace_indexes)

    def _perform_plotly_restyle(self, restyle_data, trace_indexes):
        """
        Perform a restyle operation on the figure's traces data and return
        the changes that were applied

        Parameters
        ----------
        restyle_data : dict[str, any]
            See docstring for plotly_restyle
        trace_indexes : list[int]
            List of trace indexes that restyle operation applies to
        Returns
        -------
        restyle_changes: dict[str, any]
            Subset of restyle_data including only the keys / values that
            resulted in a change to the figure's traces data
        """
        # Initialize restyle changes
        # --------------------------
        # This will be a subset of the restyle_data including only the
        # keys / values that are changed in the figure's trace data
        restyle_changes = {}

        # Process each key
        # ----------------
        for key_path_str, v in restyle_data.items():

            # Track whether any of the new values are cause a change in
            # self._data
            any_vals_changed = False
            for i, trace_ind in enumerate(trace_indexes):
                if trace_ind >= len(self._data):
                    raise ValueError(
                        'Trace index {trace_ind} out of range'.format(
                            trace_ind=trace_ind))

                # Get new value for this particular trace
                trace_v = v[i % len(v)] if isinstance(v, list) else v

                if trace_v is not Undefined:

                    # Get trace being updated
                    trace_obj = self.data[trace_ind]

                    # Validate key_path_str
                    if not BaseFigure._is_key_path_compatible(
                            key_path_str, trace_obj):

                        trace_class = trace_obj.__class__.__name__
                        raise ValueError("""
Invalid property path '{key_path_str}' for trace class {trace_class}
""".format(key_path_str=key_path_str, trace_class=trace_class))

                    # Apply set operation for this trace and thist value
                    val_changed = BaseFigure._set_in(self._data[trace_ind],
                                                     key_path_str,
                                                     trace_v)

                    # Update any_vals_changed status
                    any_vals_changed = (any_vals_changed or val_changed)

            if any_vals_changed:
                restyle_changes[key_path_str] = v

        return restyle_changes

    def _restyle_child(self, child, key_path_str, val):
        """
        Process restyle operation on a child trace object

        Note: This method name/signature must match the one in
        BasePlotlyType. BasePlotlyType objects call their parent's
        _restyle_child method without knowing whether their parent is a
        BasePlotlyType or a BaseFigure.

        Parameters
        ----------
        child : BaseTraceType
            Child being restyled
        key_path_str : str
            A key path string (e.g. 'foo.bar[0]')
        val
            Restyle value

        Returns
        -------
        None
        """

        # Compute trace index
        # -------------------
        trace_index = BaseFigure._index_is(self.data, child)

        # Not in batch mode
        # -----------------
        # Dispatch change callbacks and send restyle message
        if not self._in_batch_mode:
            send_val = [val]
            restyle = {key_path_str: send_val}
            self._send_restyle_msg(restyle, trace_indexes=trace_index)
            self._dispatch_trace_change_callbacks(restyle, [trace_index])

        # In batch mode
        # -------------
        # Add key_path_str/val to saved batch edits
        else:
            if trace_index not in self._batch_trace_edits:
                self._batch_trace_edits[trace_index] = {}
            self._batch_trace_edits[trace_index][key_path_str] = val

    def _normalize_trace_indexes(self, trace_indexes):
        """
        Input trace index specification and return list of the specified trace
        indexes

        Parameters
        ----------
        trace_indexes : None or int or list[int]

        Returns
        -------
        list[int]
        """
        if trace_indexes is None:
            trace_indexes = list(range(len(self.data)))
        if not isinstance(trace_indexes, (list, tuple)):
            trace_indexes = [trace_indexes]
        return list(trace_indexes)

    @staticmethod
    def _str_to_dict_path(key_path_str):
        """
        Convert a key path string into a tuple of key path elements

        Parameters
        ----------
        key_path_str : str
            Key path string, where nested keys are joined on '.' characters
            and array indexes are specified using brackets
            (e.g. 'foo.bar[1]')
        Returns
        -------
        tuple[str | int]
        """
        if isinstance(key_path_str, tuple):
            # Nothing to do
            return key_path_str
        else:
            # Split string on periods. e.g. 'foo.bar[1]' -> ['foo', 'bar[1]']
            key_path = key_path_str.split('.')

            # Split out bracket indexes.
            # e.g. ['foo', 'bar[1]'] -> ['foo', 'bar', '1']
            bracket_re = re.compile('(.*)\[(\d+)\]')
            key_path2 = []
            for key in key_path:
                match = bracket_re.fullmatch(key)
                if match:
                    key_path2.extend(match.groups())
                else:
                    key_path2.append(key)

            # Convert elements to ints if possible.
            # e.g. ['foo', 'bar', '0'] -> ['foo', 'bar', 0]
            for i in range(len(key_path2)):
                try:
                    key_path2[i] = int(key_path2[i])
                except ValueError as _:
                    pass

            return tuple(key_path2)

    @staticmethod
    def _set_in(d, key_path_str, v):
        """
        Set a value in a nested dict using a key path string
        (e.g. 'foo.bar[0]')

        Parameters
        ----------
        d : dict
            Input dict to set property in
        key_path_str : str
            Key path string, where nested keys are joined on '.' characters
            and array indexes are specified using brackets
            (e.g. 'foo.bar[1]')
        v
            New value
        Returns
        -------
        bool
            True if set resulted in modification of dict (i.e. v was not
            already present at the specified location), False otherwise.
        """

        # Validate inputs
        # ---------------
        assert isinstance(d, dict)

        # Compute key path
        # ----------------
        # Convert the key_path_str into a tuple of key paths
        # e.g. 'foo.bar[0]' -> ('foo', 'bar', 0)
        key_path = BaseFigure._str_to_dict_path(key_path_str)

        # Initialize val_parent
        # ---------------------
        # This variable will be assigned to the parent of the next key path
        # element currently being processed
        val_parent = d

        # Initialize parent dict or list of value to be assigned
        # -----------------------------------------------------
        for kp, key_path_el in enumerate(key_path[:-1]):

            # Extend val_parent list if needed
            if (isinstance(val_parent, list) and
                    isinstance(key_path_el, int)):
                while len(val_parent) <= key_path_el:
                    val_parent.append(None)

            elif (isinstance(val_parent, dict) and
                  key_path_el not in val_parent):
                if isinstance(key_path[kp + 1], int):
                    val_parent[key_path_el] = []
                else:
                    val_parent[key_path_el] = {}

            val_parent = val_parent[key_path_el]

        # Assign value to to final parent dict or list
        # --------------------------------------------
        # ### Get reference to final key path element ###
        last_key = key_path[-1]

        # ### Track whether assignment alters parent ###
        val_changed = False

        # v is Undefined
        # --------------
        # Don't alter val_parent
        if v is Undefined:
            pass

        # v is None
        # ---------
        # Check whether we can remove key from parent
        elif v is None:
            if isinstance(val_parent, dict):
                if last_key in val_parent:
                    # Parent is a dict and has last_key as a current key so
                    # we can pop the key, which alters parent
                    val_parent.pop(last_key)
                    val_changed = True
            elif isinstance(val_parent, list):
                if (isinstance(last_key, int) and
                        0 <= last_key < len(val_parent)):
                    # Parent is a list and last_key is a valid index so we
                    # can set the element value to None
                    val_parent[last_key] = None
                    val_changed = True
            else:
                # Unsupported parent type (numpy array for example)
                raise ValueError("""
    Cannot remove element of type {typ} at location {raw_key}"""
                                 .format(typ=type(val_parent),
                                         raw_key=key_path_str))
        # v is a valid value
        # ------------------
        # Check whether parent should be updated
        else:
            if isinstance(val_parent, dict):
                if (last_key not in val_parent
                        or not BasePlotlyType._vals_equal(
                            val_parent[last_key], v)):
                    # Parent is a dict and does not already contain the
                    # value v at key last_key
                    val_parent[last_key] = v
                    val_changed = True
            elif isinstance(val_parent, list):
                if isinstance(last_key, int):
                    # Extend list with Nones if needed so that last_key is
                    # in bounds
                    while len(val_parent) <= last_key:
                        val_parent.append(None)

                    if not BasePlotlyType._vals_equal(
                            val_parent[last_key], v):
                        # Parent is a list and does not already contain the
                        # value v at index last_key
                        val_parent[last_key] = v
                        val_changed = True
            else:
                # Unsupported parent type (numpy array for example)
                raise ValueError("""
    Cannot set element of type {typ} at location {raw_key}"""
                                 .format(typ=type(val_parent),
                                         raw_key=key_path_str))
        return val_changed

    # Add traces
    # ----------
    @staticmethod
    def _raise_invalid_rows_cols(name, n, invalid):
        rows_err_msg = """
        If specified, the {name} parameter must be a list or tuple of integers
        of length {n} (The number of traces being added)

        Received: {invalid}
        """.format(name=name, n=n, invalid=invalid)

        raise ValueError(rows_err_msg)

    @staticmethod
    def _validate_rows_cols(name, n, vals):
        if vals is None:
            pass
        elif isinstance(vals, (list, tuple)):
            if len(vals) != n:
                BaseFigure._raise_invalid_rows_cols(
                    name=name, n=n, invalid=vals)

            if [r for r in vals if not isinstance(r, int)]:
                BaseFigure._raise_invalid_rows_cols(
                    name=name, n=n, invalid=vals)
        else:
            BaseFigure._raise_invalid_rows_cols(name=name, n=n, invalid=vals)

    def add_trace(self, trace, row=None, col=None):
        """
        Add a trace to the figure

        Parameters
        ----------
        trace : BaseTraceType or dict
            Either:
              - An instances of a trace classe from the plotly.graph_objs
                package (e.g plotly.graph_objs.Scatter, plotly.graph_objs.Bar)
              - or a dicts where:

                  - The 'type' property specifies the trace type (e.g.
                    'scatter', 'bar', 'area', etc.). If the dict has no 'type'
                    property then 'scatter' is assumed.
                  - All remaining properties are passed to the constructor
                    of the specified trace type.

        row : int or None (default)
            Subplot row index (starting from 1) for the trace to be added.
            Only valid if figure was created using
            `plotly.tools.make_subplots`
        col : int or None (default)
            Subplot col index (starting from 1) for the trace to be added.
            Only valid if figure was created using
            `plotly.tools.make_subplots`

        Returns
        -------
        BaseTraceType
            The newly added trace

        Examples
        --------
        >>> from plotly import tools
        >>> import plotly.graph_objs as go

        Add two Scatter traces to a figure
        >>> fig = go.Figure()
        >>> fig.add_trace(go.Scatter(x=[1,2,3], y=[2,1,2]))
        >>> fig.add_trace(go.Scatter(x=[1,2,3], y=[2,1,2]))


        Add two Scatter traces to vertically stacked subplots
        >>> fig = tools.make_subplots(rows=2)
        This is the format of your plot grid:
        [ (1,1) x1,y1 ]
        [ (2,1) x2,y2 ]

        >>> fig.add_trace(go.Scatter(x=[1,2,3], y=[2,1,2]), row=1, col=1)
        >>> fig.add_trace(go.Scatter(x=[1,2,3], y=[2,1,2]), row=2, col=1)
        """
        # Validate row/col
        if row is not None and not isinstance(row, int):
            pass

        if col is not None and not isinstance(col, int):
            pass

        # Make sure we have both row and col or neither
        if row is not None and col is None:
            raise ValueError(
                'Received row parameter but not col.\n'
                'row and col must be specified together')
        elif col is not None and row is None:
            raise ValueError(
                'Received col parameter but not row.\n'
                'row and col must be specified together')

        return self.add_traces(data=[trace],
                               rows=[row] if row is not None else None,
                               cols=[col] if col is not None else None
                               )[0]

    def add_traces(self, data, rows=None, cols=None):
        """
        Add traces to the figure

        Parameters
        ----------
        data : list[BaseTraceType or dict]
            A list of trace specifications to be added.
            Trace specifications may be either:

              - Instances of trace classes from the plotly.graph_objs
                package (e.g plotly.graph_objs.Scatter, plotly.graph_objs.Bar)
              - Dicts where:

                  - The 'type' property specifies the trace type (e.g.
                    'scatter', 'bar', 'area', etc.). If the dict has no 'type'
                    property then 'scatter' is assumed.
                  - All remaining properties are passed to the constructor
                    of the specified trace type.

        rows : None or list[int] (default None)
            List of subplot row indexes (starting from 1) for the traces to be
            added. Only valid if figure was created using
            `plotly.tools.make_subplots`
        cols : None or list[int] (default None)
            List of subplot column indexes (starting from 1) for the traces
            to be added. Only valid if figure was created using
            `plotly.tools.make_subplots`

        Returns
        -------
        tuple[BaseTraceType]
            Tuple of the newly added traces

        Examples
        --------
        >>> from plotly import tools
        >>> import plotly.graph_objs as go

        Add two Scatter traces to a figure
        >>> fig = go.Figure()
        >>> fig.add_traces([go.Scatter(x=[1,2,3], y=[2,1,2]),
        ...                 go.Scatter(x=[1,2,3], y=[2,1,2])])

        Add two Scatter traces to vertically stacked subplots
        >>> fig = tools.make_subplots(rows=2)
        This is the format of your plot grid:
        [ (1,1) x1,y1 ]
        [ (2,1) x2,y2 ]

        >>> fig.add_traces([go.Scatter(x=[1,2,3], y=[2,1,2]),
        ...                 go.Scatter(x=[1,2,3], y=[2,1,2])],
        ...                 rows=[1, 2], cols=[1, 1])
        """

        if self._in_batch_mode:
            self._batch_layout_edits.clear()
            self._batch_trace_edits.clear()
            raise ValueError('Traces may not be added in a batch context')

        # Validate traces
        data = self._data_validator.validate_coerce(data)

        # Validate rows / cols
        n = len(data)
        BaseFigure._validate_rows_cols('rows', n, rows)
        BaseFigure._validate_rows_cols('cols', n, cols)

        # Make sure we have both rows and cols or neither
        if rows is not None and cols is None:
            raise ValueError(
                'Received rows parameter but not cols.\n'
                'rows and cols must be specified together')
        elif cols is not None and rows is None:
            raise ValueError(
                'Received cols parameter but not rows.\n'
                'rows and cols must be specified together')

        # Apply rows / cols
        if rows is not None:
            for trace, row, col in zip(data, rows, cols):
                self._set_trace_grid_position(trace, row, col)

        # Make deep copy of trace data (Optimize later if needed)
        new_traces_data = [deepcopy(trace._props) for trace in data]

        # Update trace parent
        for trace in data:
            trace._parent = self
            trace._orphan_props.clear()

        # Update python side
        #  Use extend instead of assignment so we don't trigger serialization
        self._data.extend(new_traces_data)
        self._data_defaults = self._data_defaults + [{} for _ in data]
        self._data_objs = self._data_objs + data

        # Update messages
        self._send_addTraces_msg(new_traces_data)

        return data

    # Subplots
    # --------
    def print_grid(self):
        """
        Print a visual layout of the figure's axes arrangement.
        This is only valid for figures that are created
        with plotly.tools.make_subplots.
        """
        if self._grid_str is None:
            raise Exception("Use plotly.tools.make_subplots "
                            "to create a subplot grid.")
        print(self._grid_str)

    def append_trace(self, trace, row, col):
        """
        Add a trace to the figure bound to axes at the specified row,
        col index.

        A row, col index grid is generated for figures created with
        plotly.tools.make_subplots, and can be viewed with the `print_grid`
        method

        Parameters
        ----------
        trace
            The data trace to be bound
        row: int
            Subplot row index (see Figure.print_grid)
        col: int
            Subplot column index (see Figure.print_grid)

        Examples
        --------
        >>> from plotly import tools
        >>> import plotly.graph_objs as go
        # stack two subplots vertically
        >>> fig = tools.make_subplots(rows=2)
        This is the format of your plot grid:
        [ (1,1) x1,y1 ]
        [ (2,1) x2,y2 ]

        >>> fig.append_trace(go.Scatter(x=[1,2,3], y=[2,1,2]), row=1, col=1)
        >>> fig.append_trace(go.Scatter(x=[1,2,3], y=[2,1,2]), row=2, col=1)
        """
        warnings.warn("""\
The append_trace method is deprecated and will be removed in a future version. 
Please use the add_trace method with the row and col parameters.
""", DeprecationWarning)

        self.add_trace(trace=trace, row=row, col=col)

    def _set_trace_grid_position(self, trace, row, col):
        try:
            grid_ref = self._grid_ref
        except AttributeError:
            raise Exception("In order to use Figure.append_trace, "
                            "you must first use "
                            "plotly.tools.make_subplots "
                            "to create a subplot grid.")
        if row <= 0:
            raise Exception("Row value is out of range. "
                            "Note: the starting cell is (1, 1)")
        if col <= 0:
            raise Exception("Col value is out of range. "
                            "Note: the starting cell is (1, 1)")
        try:
            ref = grid_ref[row - 1][col - 1]
        except IndexError:
            raise Exception("The (row, col) pair sent is out of "
                            "range. Use Figure.print_grid to view the "
                            "subplot grid. ")
        if 'scene' in ref[0]:
            trace['scene'] = ref[0]
            if ref[0] not in self['layout']:
                raise Exception("Something went wrong. "
                                "The scene object for ({r},{c}) "
                                "subplot cell "
                                "got deleted.".format(r=row, c=col))
        else:
            xaxis_key = "xaxis{ref}".format(ref=ref[0][1:])
            yaxis_key = "yaxis{ref}".format(ref=ref[1][1:])
            if (xaxis_key not in self['layout']
                    or yaxis_key not in self['layout']):
                raise Exception("Something went wrong. "
                                "An axis object for ({r},{c}) subplot "
                                "cell got deleted.".format(r=row, c=col))
            trace['xaxis'] = ref[0]
            trace['yaxis'] = ref[1]

    # Child property operations
    # -------------------------
    def _get_child_props(self, child):
        """
        Return the properties dict for a child trace or child layout

        Note: this method must match the name/signature of one on
        BasePlotlyType

        Parameters
        ----------
        child : BaseTraceType | BaseLayoutType

        Returns
        -------
        dict
        """
        # Try to find index of child as a trace
        # -------------------------------------
        try:
            trace_index = BaseFigure._index_is(self.data, child)
        except ValueError as _:
            trace_index = None

        # Child is a trace
        # ----------------
        if trace_index is not None:
            return self._data[trace_index]

        # Child is the layout
        # -------------------
        elif child is self.layout:
            return self._layout

        # Unknown child
        # -------------
        else:
            raise ValueError('Unrecognized child: %s' % child)

    def _get_child_prop_defaults(self, child):
        """
        Return the default properties dict for a child trace or child layout

        Note: this method must match the name/signature of one on
        BasePlotlyType

        Parameters
        ----------
        child : BaseTraceType | BaseLayoutType

        Returns
        -------
        dict
        """
        # Try to find index of child as a trace
        # -------------------------------------
        try:
            trace_index = BaseFigure._index_is(self.data, child)
        except ValueError as _:
            trace_index = None

        # Child is a trace
        # ----------------
        if trace_index is not None:
            return self._data_defaults[trace_index]

        # Child is the layout
        # -------------------
        elif child is self.layout:
            return self._layout_defaults

        # Unknown child
        # -------------
        else:
            raise ValueError('Unrecognized child: %s' % child)

    def _init_child_props(self, child):
        """
        Initialize the properites dict for a child trace or layout

        Note: this method must match the name/signature of one on
        BasePlotlyType

        Parameters
        ----------
        child : BaseTraceType | BaseLayoutType

        Returns
        -------
        None
        """
        # layout and traces dict are initialize when figure is constructed
        # and when new traces are added to the figure
        pass

    # Layout
    # ------
    @property
    def layout(self):
        """
        The `layout` property of the figure

        Returns
        -------
        plotly.graph_objs.Layout
        """
        return self['layout']

    @layout.setter
    def layout(self, new_layout):

        # Validate new layout
        # -------------------
        new_layout = self._layout_validator.validate_coerce(new_layout)
        new_layout_data = deepcopy(new_layout._props)

        # Unparent current layout
        # -----------------------
        if self._layout_obj:
            old_layout_data = deepcopy(self._layout_obj._props)
            self._layout_obj._orphan_props.update(old_layout_data)
            self._layout_obj._parent = None

        # Parent new layout
        # -----------------
        self._layout = new_layout_data
        new_layout._parent = self
        new_layout._orphan_props.clear()
        self._layout_obj = new_layout

        # Notify JS side
        self._send_relayout_msg(new_layout_data)

    def plotly_relayout(self, relayout_data, **kwargs):
        """
        Perform a Plotly relayout operation on the figure's layout

        Parameters
        ----------
        relayout_data : dict
            Dict of layout updates

            dict keys are strings that specify the properties to be updated.
            Nested properties are expressed by joining successive keys on
            '.' characters (e.g. 'xaxis.range')

            dict values are the values to use to update the layout.

        Returns
        -------
        None
        """

        # Handle source_view_id
        # ---------------------
        # If not None, the source_view_id is the UID of the frontend
        # Plotly.js view that initially triggered this relayout operation
        # (e.g. the user clicked on the toolbar to change the drag mode
        # from zoom to pan). We pass this UID along so that the frontend
        # views can determine whether they need to apply the relayout
        # operation on themselves.
        if 'source_view_id' in kwargs:
            msg_kwargs = {'source_view_id': kwargs['source_view_id']}
        else:
            msg_kwargs = {}

        # Perform relayout operation on layout dict
        # -----------------------------------------
        relayout_changes = self._perform_plotly_relayout(relayout_data)
        if relayout_changes:
            # The relayout operation resulted in a change to some layout
            # properties, so we dispatch change callbacks and send the
            # relayout message to the frontend (if any)
            self._send_relayout_msg(
                relayout_changes, **msg_kwargs)

            self._dispatch_layout_change_callbacks(relayout_changes)

    def _perform_plotly_relayout(self, relayout_data):
        """
        Perform a relayout operation on the figure's layout data and return
        the changes that were applied

        Parameters
        ----------
        relayout_data : dict[str, any]
            See the docstring for plotly_relayout
        Returns
        -------
        relayout_changes: dict[str, any]
            Subset of relayout_data including only the keys / values that
            resulted in a change to the figure's layout data
        """
        # Initialize relayout changes
        # ---------------------------
        # This will be a subset of the relayout_data including only the
        # keys / values that are changed in the figure's layout data
        relayout_changes = {}

        # Process each key
        # ----------------
        for key_path_str, v in relayout_data.items():

            if not BaseFigure._is_key_path_compatible(
                    key_path_str, self.layout):

                raise ValueError("""
Invalid property path '{key_path_str}' for layout
""".format(key_path_str=key_path_str))

            # Apply set operation on the layout dict
            val_changed = BaseFigure._set_in(self._layout, key_path_str, v)

            if val_changed:
                relayout_changes[key_path_str] = v

        return relayout_changes

    @staticmethod
    def _is_key_path_compatible(key_path_str, plotly_obj):
        """
        Return whether the specifieid key path string is compatible with
        the specified plotly object for the purpose of relayout/restyle
        operation
        """

        # Convert string to tuple of path components
        # e.g. 'foo[0].bar[1]' -> ('foo', 0, 'bar', 1)
        key_path_tuple = BaseFigure._str_to_dict_path(key_path_str)

        # Remove trailing integer component
        # e.g. ('foo', 0, 'bar', 1) -> ('foo', 0, 'bar')
        # We do this because it's fine for relayout/restyle to create new
        # elements in the final array in the path.
        if isinstance(key_path_tuple[-1], int):
            key_path_tuple = key_path_tuple[:-1]

        # Test whether modified key path tuple is in plotly_obj
        return key_path_tuple in plotly_obj

    def _relayout_child(self, child, key_path_str, val):
        """
        Process relayout operation on child layout object

        Parameters
        ----------
        child : BaseLayoutType
            The figure's layout
        key_path_str :
            A key path string (e.g. 'foo.bar[0]')
        val
            Relayout value

        Returns
        -------
        None
        """

        # Validate input
        # --------------
        assert child is self.layout

        # Not in batch mode
        # -------------
        # Dispatch change callbacks and send relayout message
        if not self._in_batch_mode:
            relayout_msg = {key_path_str: val}
            self._send_relayout_msg(relayout_msg)
            self._dispatch_layout_change_callbacks(relayout_msg)

        # In batch mode
        # -------------
        # Add key_path_str/val to saved batch edits
        else:
            self._batch_layout_edits[key_path_str] = val

    # Dispatch change callbacks
    # -------------------------
    @staticmethod
    def _build_dispatch_plan(key_path_strs):
        """
        Build a dispatch plan for a list of key path strings

        A dispatch plan is a dict:
           - *from* path tuples that reference an object that has descendants
             that are referenced in `key_path_strs`.
           - *to* sets of tuples that correspond to descendants of the object
             above.

        Parameters
        ----------
        key_path_strs : list[str]
            List of key path strings. For example:

            ['xaxis.rangeselector.font.color', 'xaxis.rangeselector.bgcolor']

        Returns
        -------
        dispatch_plan: dict[tuple[str|int], set[tuple[str|int]]]

        Examples
        --------
        >>> key_path_strs = ['xaxis.rangeselector.font.color',
        ...                  'xaxis.rangeselector.bgcolor']

        >>> BaseFigure._build_dispatch_plan(key_path_strs)
            {(): {('xaxis',),
                  ('xaxis', 'rangeselector'),
                  ('xaxis', 'rangeselector', 'bgcolor'),
                  ('xaxis', 'rangeselector', 'font'),
                  ('xaxis', 'rangeselector', 'font', 'color')},
             ('xaxis',): {('rangeselector',),
                          ('rangeselector', 'bgcolor'),
                          ('rangeselector', 'font'),
                          ('rangeselector', 'font', 'color')},
             ('xaxis', 'rangeselector'): {('bgcolor',),
                                          ('font',),
                                          ('font', 'color')},
             ('xaxis', 'rangeselector', 'font'): {('color',)}}
        """
        dispatch_plan = {}

        for key_path_str in key_path_strs:

            key_path = BaseFigure._str_to_dict_path(key_path_str)
            key_path_so_far = ()
            keys_left = key_path

            # Iterate down the key path
            for next_key in key_path:
                if key_path_so_far not in dispatch_plan:
                    dispatch_plan[key_path_so_far] = set()

                to_add = [keys_left[:i+1] for i in range(len(keys_left))]
                dispatch_plan[key_path_so_far].update(to_add)

                key_path_so_far = key_path_so_far + (next_key, )
                keys_left = keys_left[1:]

        return dispatch_plan

    def _dispatch_layout_change_callbacks(self, relayout_data):
        """
        Dispatch property change callbacks given relayout_data

        Parameters
        ----------
        relayout_data : dict[str, any]
            See docstring for plotly_relayout.

        Returns
        -------
        None
        """
        # Build dispatch plan
        # -------------------
        key_path_strs = list(relayout_data.keys())
        dispatch_plan = BaseFigure._build_dispatch_plan(key_path_strs)

        # Dispatch changes to each layout objects
        # ---------------------------------------
        for path_tuple, changed_paths in dispatch_plan.items():
            dispatch_obj = self.layout[path_tuple]
            if isinstance(dispatch_obj, BasePlotlyType):
                dispatch_obj._dispatch_change_callbacks(changed_paths)

    def _dispatch_trace_change_callbacks(self, restyle_data, trace_indexes):
        """
        Dispatch property change callbacks given restyle_data

        Parameters
        ----------
        restyle_data : dict[str, any]
            See docstring for plotly_restyle.

        trace_indexes : list[int]
            List of trace indexes that restyle operation applied to

        Returns
        -------
        None
        """

        # Build dispatch plan
        # -------------------
        key_path_strs = list(restyle_data.keys())
        dispatch_plan = BaseFigure._build_dispatch_plan(key_path_strs)

        # Dispatch changes to each object in each trace
        # ---------------------------------------------
        for path_tuple, changed_paths in dispatch_plan.items():
            for trace_ind in trace_indexes:
                trace = self.data[trace_ind]
                dispatch_obj = trace[path_tuple]
                if isinstance(dispatch_obj, BasePlotlyType):
                    dispatch_obj._dispatch_change_callbacks(changed_paths)

    # Frames
    # ------
    @property
    def frames(self):
        """
        The `frames` property is a tuple of the figure's frame objects

        Returns
        -------
        tuple[plotly.graph_objs.Frame]
        """
        return self['frames']

    @frames.setter
    def frames(self, new_frames):
        # Note: Frames are not supported by the FigureWidget subclass so we
        # only validate coerce the frames. We don't emit any events on frame
        # changes, and we don't reparent the frames.

        # Validate frames
        self._frame_objs = self._frames_validator.validate_coerce(new_frames)

    # Update
    # ------
    def plotly_update(self,
                      restyle_data=None,
                      relayout_data=None,
                      trace_indexes=None,
                      **kwargs):
        """
        Perform a Plotly update operation on the figure.

        Note: This operation both mutates and returns the figure

        Parameters
        ----------
        restyle_data : dict
            Traces update specification. See the docstring for the
            `plotly_restyle` method for details
        relayout_data : dict
            Layout update specification. See the docstring for the
            `plotly_relayout` method for details
        trace_indexes :
            Trace index, or list of trace indexes, that the update operation
            applies to. Defaults to all trace indexes.

        Returns
        -------
        BaseFigure
            None
        """

        # Handle source_view_id
        # ---------------------
        # If not None, the source_view_id is the UID of the frontend
        # Plotly.js view that initially triggered this update operation
        # (e.g. the user clicked a button that triggered an update
        # operation). We pass this UID along so that the frontend views can
        # determine whether they need to apply the update operation on
        # themselves.
        if 'source_view_id' in kwargs:
            msg_kwargs = {'source_view_id': kwargs['source_view_id']}
        else:
            msg_kwargs = {}

        # Perform update operation
        # ------------------------
        # This updates the _data and _layout dicts, and returns the changes
        # to the traces (restyle_changes) and layout (relayout_changes)
        (restyle_changes,
         relayout_changes,
         trace_indexes) = self._perform_plotly_update(
            restyle_data=restyle_data,
            relayout_data=relayout_data,
            trace_indexes=trace_indexes)

        # Send update message
        # -------------------
        # Send a plotly_update message to the frontend (if any)
        if restyle_changes or relayout_changes:
            self._send_update_msg(
                restyle_data=restyle_changes,
                relayout_data=relayout_changes,
                trace_indexes=trace_indexes,
                **msg_kwargs)

        # Dispatch changes
        # ----------------
        # ### Dispatch restyle changes ###
        if restyle_changes:
            self._dispatch_trace_change_callbacks(
                restyle_changes, trace_indexes)

        # ### Dispatch relayout changes ###
        if relayout_changes:
            self._dispatch_layout_change_callbacks(relayout_changes)

    def _perform_plotly_update(self, restyle_data=None, relayout_data=None,
                               trace_indexes=None):

        # Check for early exist
        # ---------------------
        if not restyle_data and not relayout_data:
            # Nothing to do
            return None, None, None

        # Normalize input
        # ---------------
        if restyle_data is None:
            restyle_data = {}
        if relayout_data is None:
            relayout_data = {}

        trace_indexes = self._normalize_trace_indexes(trace_indexes)

        # Perform relayout
        # ----------------
        relayout_changes = self._perform_plotly_relayout(relayout_data)

        # Perform restyle
        # ---------------
        restyle_changes = self._perform_plotly_restyle(
            restyle_data, trace_indexes)

        # Return changes
        # --------------
        return restyle_changes, relayout_changes, trace_indexes

    # Plotly message stubs
    # --------------------
    # send-message stubs that may be overridden by the widget subclass
    def _send_addTraces_msg(self, new_traces_data):
        pass

    def _send_moveTraces_msg(self, current_inds, new_inds):
        pass

    def _send_deleteTraces_msg(self, delete_inds):
        pass

    def _send_restyle_msg(self, style, trace_indexes=None,
                          source_view_id=None):
        pass

    def _send_relayout_msg(self, layout, source_view_id=None):
        pass

    def _send_update_msg(self,
                         restyle_data,
                         relayout_data,
                         trace_indexes=None,
                         source_view_id=None):
        pass

    def _send_animate_msg(self,
                          styles_data,
                          relayout_data,
                          trace_indexes,
                          animation_opts):
        pass

    # Context managers
    # ----------------
    @contextmanager
    def batch_update(self):
        """
        A context manager that batches up trace and layout assignment
        operations into a singe plotly_update message that is executed when
        the context exits.

        Examples
        --------
        For example, suppose we have a figure widget, `fig`, with a single
        trace.
        >>> import plotly.graph_objs as go
        >>> fig = go.FigureWidget(data=[{'y': [3, 4, 2]}])

        If we want to update the xaxis range, the yaxis range, and the
        marker color, we could do so using a series of three property
        assignments as follows:

        >>> fig.layout.xaxis.range = [0, 5]
        >>> fig.layout.yaxis.range = [0, 10]
        >>> fig.data[0].marker.color = 'green'

        This will work, however it will result in three messages being
        sent to the front end (two relayout messages for the axis range
        updates followed by one restyle message for the marker color
        update). This can cause the plot to appear to stutter as the
        three updates are applied incrementally.

        We can avoid this problem by performing these three assignments in a
        `batch_update` context as follows:

        >>> with fig.batch_update():
        ...     fig.layout.xaxis.range = [0, 5]
        ...     fig.layout.yaxis.range = [0, 10]
        ...     fig.data[0].marker.color = 'green'

        Now, these three property updates will be sent to the frontend in a
        single update message, and they will be applied by the front end
        simultaneously.
        """
        if self._in_batch_mode is True:
            yield
        else:
            try:
                self._in_batch_mode = True
                yield
            finally:
                # ### Disable batch mode ###
                self._in_batch_mode = False

                # ### Build plotly_update params ###
                (restyle_data,
                 relayout_data,
                 trace_indexes) = self._build_update_params_from_batch()

                # ### Call plotly_update ###
                self.plotly_update(
                    restyle_data=restyle_data,
                    relayout_data=relayout_data,
                    trace_indexes=trace_indexes)

                # ### Clear out saved batch edits ###
                self._batch_layout_edits.clear()
                self._batch_trace_edits.clear()

    def _build_update_params_from_batch(self):
        """
        Convert `_batch_trace_edits` and `_batch_layout_edits` into the
        `restyle_data`, `relayout_data`, and `trace_indexes` params accepted
        by the `plotly_update` method.

        Returns
        -------
        (dict, dict, list[int])
        """

        # Handle Style / Trace Indexes
        # ----------------------------
        batch_style_commands = self._batch_trace_edits
        trace_indexes = sorted(
            set([trace_ind for trace_ind in batch_style_commands]))

        all_props = sorted(
            set([
                prop for trace_style in self._batch_trace_edits.values()
                for prop in trace_style
            ]))

        # Initialize restyle_data dict with all values undefined
        restyle_data = {
            prop: [Undefined for _ in range(len(trace_indexes))]
            for prop in all_props
        }

        # Fill in values
        for trace_ind, trace_style in batch_style_commands.items():
            for trace_prop, trace_val in trace_style.items():
                restyle_trace_index = trace_indexes.index(trace_ind)
                restyle_data[trace_prop][restyle_trace_index] = trace_val

        # Handle Layout
        # -------------
        relayout_data = self._batch_layout_edits

        # Return plotly_update params
        # ---------------------------
        return restyle_data, relayout_data, trace_indexes

    @contextmanager
    def batch_animate(self, duration=500, easing="cubic-in-out"):
        """
        Context manager to animate trace / layout updates

        Parameters
        ----------
        duration : number
            The duration of the transition, in milliseconds.
            If equal to zero, updates are synchronous.
        easing : string
            The easing function used for the transition.
            One of:
                - linear
                - quad
                - cubic
                - sin
                - exp
                - circle
                - elastic
                - back
                - bounce
                - linear-in
                - quad-in
                - cubic-in
                - sin-in
                - exp-in
                - circle-in
                - elastic-in
                - back-in
                - bounce-in
                - linear-out
                - quad-out
                - cubic-out
                - sin-out
                - exp-out
                - circle-out
                - elastic-out
                - back-out
                - bounce-out
                - linear-in-out
                - quad-in-out
                - cubic-in-out
                - sin-in-out
                - exp-in-out
                - circle-in-out
                - elastic-in-out
                - back-in-out
                - bounce-in-out

        Examples
        --------
        Suppose we have a figure widget, `fig`, with a single trace.

        >>> import plotly.graph_objs as go
        >>> fig = go.FigureWidget(data=[{'y': [3, 4, 2]}])

        1) Animate a change in the xaxis and yaxis ranges using default
        duration and easing parameters.

        >>> with fig.batch_animate():
        ...     fig.layout.xaxis.range = [0, 5]
        ...     fig.layout.yaxis.range = [0, 10]

        2) Animate a change in the size and color of the trace's markers
        over 2 seconds using the elastic-in-out easing method
        >>> with fig.batch_update(duration=2000, easing='elastic-in-out'):
        ...     fig.data[0].marker.color = 'green'
        ...     fig.data[0].marker.size = 20
        """

        # Validate inputs
        # ---------------
        duration = self._animation_duration_validator.validate_coerce(duration)
        easing = self._animation_easing_validator.validate_coerce(easing)

        if self._in_batch_mode is True:
            yield
        else:
            try:
                self._in_batch_mode = True
                yield
            finally:
                # Exit batch mode
                # ---------------
                self._in_batch_mode = False

                # Apply batch animate
                # -------------------
                self._perform_batch_animate({
                    'transition': {
                        'duration': duration,
                        'easing': easing
                    },
                    'frame': {
                        'duration': duration
                    }
                })

    def _perform_batch_animate(self, animation_opts):
        """
        Perform the batch animate operation

        This method should be called with the batch_animate() context
        manager exits.

        Parameters
        ----------
        animation_opts : dict
            Animation options as accepted by frontend Plotly.animation command

        Returns
        -------
        None
        """
        # Apply commands to internal dictionaries as an update
        # ----------------------------------------------------
        (restyle_data,
         relayout_data,
         trace_indexes) = self._build_update_params_from_batch()

        (restyle_changes,
         relayout_changes,
         trace_indexes) = self._perform_plotly_update(restyle_data,
                                                      relayout_data,
                                                      trace_indexes)

        # Convert style / trace_indexes into animate form
        # -----------------------------------------------
        if self._batch_trace_edits:
            animate_styles, animate_trace_indexes = zip(
                *[(trace_style, trace_index) for trace_index, trace_style in
                  self._batch_trace_edits.items()])
        else:
            animate_styles, animate_trace_indexes = {}, []

        animate_layout = copy(self._batch_layout_edits)

        # Send animate message
        # --------------------
        # Sends animate message to the front end (if any)
        self._send_animate_msg(
            styles_data=list(animate_styles),
            relayout_data=animate_layout,
            trace_indexes=list(animate_trace_indexes),
            animation_opts=animation_opts)

        # Clear batched commands
        # ----------------------
        self._batch_layout_edits.clear()
        self._batch_trace_edits.clear()

        # Dispatch callbacks
        # ------------------
        # ### Dispatch restyle changes ###
        if restyle_changes:
            self._dispatch_trace_change_callbacks(restyle_changes,
                                                  trace_indexes)

        # ### Dispatch relayout changes ###
        if relayout_changes:
            self._dispatch_layout_change_callbacks(relayout_changes)

    # Exports
    # -------
    def to_dict(self):
        """
        Convert figure to a dictionary

        Note: the dictionary includes the properties explicitly set by the
        user, it does not include default values of unspecified properties

        Returns
        -------
        dict
        """
        # Handle data
        # -----------
        data = deepcopy(self._data)

        # Handle layout
        # -------------
        layout = deepcopy(self._layout)

        # Handle frames
        # -------------
        # Frame key is only added if there are any frames
        res = {'data': data, 'layout': layout}
        frames = deepcopy([frame._props for frame in self._frame_objs])
        if frames:
            res['frames'] = frames

        return res

    def to_plotly_json(self):
        """
        Convert figure to a JSON representation as a Python dict

        Returns
        -------
        dict
        """
        return self.to_dict()

    def save_html(self, filename, auto_open=False, responsive=False):
        """
        Save figure to a standalone html file

        Parameters
        ----------
        filename : str
            Full path and filename of the html file to be created
        auto_open : bool
            True if the the newly created HTML file be opened automatically,
            False otherwise
        responsive : bool
            True if the figure in the resulting HTML should automatically
            resize to fill the browser. If false then the width and height
            will be fixed to the figure layout's width and heigh respectively
        Returns
        -------
        None
        """
        data = self.to_dict()
        if responsive:
            if 'height' in data['layout']:
                data['layout'].pop('height')
            if 'width' in data['layout']:
                data['layout'].pop('width')
        else:
            # Assign width/height explicitly in case these were defaults
            data['layout']['height'] = self.layout.height
            data['layout']['width'] = self.layout.width

        pyo.plot(data, filename=filename, show_link=False, auto_open=auto_open)

    # Static helpers
    # --------------
    @staticmethod
    def _is_dict_list(v):
        """
        Return true of the input object is a list of dicts
        """
        return isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict)

    @staticmethod
    def _perform_update(plotly_obj, update_obj):
        """
        Helper to support the update() methods on :class:`BaseFigure` and
        :class:`BasePlotlyType`

        Parameters
        ----------
        plotly_obj : BasePlotlyType|tuple[BasePlotlyType]
            Object to up updated
        update_obj : dict|list[dict]|tuple[dict]
            When ``plotly_obj`` is an instance of :class:`BaseFigure`,
            ``update_obj`` should be a dict

            When ``plotly_obj`` is a tuple of instances of
            :class:`BasePlotlyType`, ``update_obj`` should be a tuple or list
            of dicts
        """

        if update_obj is None:
            # Nothing to do
            return
        elif isinstance(plotly_obj, BasePlotlyType):

            # Handle invalid properties
            # -------------------------
            invalid_props = [
                k for k in update_obj if k not in plotly_obj._validators
            ]

            plotly_obj._raise_on_invalid_property_error(*invalid_props)

            # Process valid properties
            # ------------------------
            for key in update_obj:
                val = update_obj[key]
                validator = plotly_obj._validators[key]

                if isinstance(validator, CompoundValidator):

                    # Update compound objects recursively
                    # plotly_obj[key].update(val)
                    BaseFigure._perform_update(
                        plotly_obj[key], val)
                elif isinstance(validator, CompoundArrayValidator):
                    BaseFigure._perform_update(
                        plotly_obj[key], val)
                else:
                    # Assign non-compound value
                    plotly_obj[key] = val

        elif isinstance(plotly_obj, tuple):

            if len(update_obj) == 0:
                # Nothing to do
                return
            else:
                for i, plotly_element in enumerate(plotly_obj):
                    if isinstance(update_obj, dict):
                        if i in update_obj:
                            update_element = update_obj[i]
                        else:
                            continue
                    else:
                        update_element = update_obj[i % len(update_obj)]
                    BaseFigure._perform_update(plotly_element, update_element)
        else:
            raise ValueError('Unexpected plotly object with type {typ}'
                             .format(typ=type(plotly_obj)))

    @staticmethod
    def _index_is(iterable, val):
        """
        Return the index of a value in an iterable using object identity
        (not object equality as is the case for list.index)

        """
        index_list = [
            i for i, curr_val in enumerate(iterable) if curr_val is val
        ]
        if not index_list:
            raise ValueError('Invalid value')

        return index_list[0]


class BasePlotlyType:
    """
    BasePlotlyType is the base class for all objects in the trace, layout,
    and frame object hierarchies
    """

    def __init__(self, plotly_name, **kwargs):
        """
        Construct a new BasePlotlyType

        Parameters
        ----------
        plotly_name : str
            The lowercase name of the plotly object
        kwargs : dict
            Invalid props/values to raise on
        """
        # Validate inputs
        # ---------------
        self._process_kwargs(**kwargs)

        # Store params
        # ------------
        self._plotly_name = plotly_name

        # Initialize properties
        # ---------------------
        # ### _validators ###
        # A dict from property names to property validators
        # type: Dict[str, BaseValidator]
        self._validators = {}

        # ### _compound_props ###
        # A dict from compound property names to compound objects
        # type: Dict[str, BasePlotlyType]
        self._compound_props = {}

        # ### _compound_array_props ###
        # A dict from compound array property names to tuples of compound
        # objects
        # type: Dict[str, Tuple[BasePlotlyType]]
        self._compound_array_props = {}

        # ### _orphan_props ###
        # A dict of properties for use while object has no parent. When
        # object has a parent, it requests its properties dict from its
        # parent and doesn't use this.
        # type: Dict
        self._orphan_props = {}

        # ### _parent ###
        # The parent of the object. May be another BasePlotlyType or it may
        # be a BaseFigure (as is the case for the Layout and Trace objects)
        # type: Union[BasePlotlyType, BaseFigure]
        self._parent = None

        # ### _change_callbacks ###
        # A dict from tuples of child property path tuples to lists
        # of callbacks that should be executed whenever any of these
        # properties is modified
        # type: Dict[Tuple[Tuple[Union[str, int]]], List[Callable]]
        self._change_callbacks = {}

    def _process_kwargs(self, **kwargs):
        """
        Process any extra kwargs that are not predefined as constructor params
        """
        self._raise_on_invalid_property_error(*kwargs.keys())

    @property
    def plotly_name(self):
        """
        The plotly name of the object

        Returns
        -------
        str
        """
        return self._plotly_name

    @property
    def _parent_path_str(self) -> str:
        """
        dot-separated path string to this object's parent.

        Returns
        -------
        str

        Examples
        --------
        >>> import plotly.graph_objs as go
        >>> go.Layout()._parent_path_str
        ''

        >>> go.layout.XAxis()._parent_path_str
        'layout'

        >>> go.layout.xaxis.rangeselector.Button()._parent_path_str
        'layout.xaxis.rangeselector'
        """
        raise NotImplementedError

    @property
    def _prop_descriptions(self) -> str:
        """
        Formatted string containing all of this obejcts child properties
        and their descriptions

        Returns
        -------
        str
        """
        raise NotImplementedError

    @property
    def _props(self):
        """
        Dictionary used to store this object properties.  When the object
        has a parent, this dict is retreived from the parent. When the
        object does not have a parent, this dict is the object's
        `_orphan_props` property

        Note: Property will return None if the object has a parent and the
        object's properties have not been initialized using the
        `_init_props` method.

        Returns
        -------
        dict|None
        """
        if self.parent is None:
            # Use orphan data
            return self._orphan_props
        else:
            # Get data from parent's dict
            return self.parent._get_child_props(self)

    def _get_child_props(self, child):
        """
        Return properties dict for child

        Parameters
        ----------
        child : BasePlotlyType

        Returns
        -------
        dict
        """
        if self._props is None:
            # If this node's properties are uninitialized then so are its
            # child's
            return None
        else:
            # ### Child a compound property ###
            if child.plotly_name in self._compound_props:
                return self._props.get(child.plotly_name, None)

            # ### Child an element of a compound array property ###
            elif child.plotly_name in self._compound_array_props:
                children = self._compound_array_props[child.plotly_name]
                child_ind = BaseFigure._index_is(children, child)
                assert child_ind is not None

                children_props = self._props.get(child.plotly_name, None)
                return (children_props[child_ind]
                        if children_props is not None and
                        len(children_props) > child_ind
                        else None)

            # ### Invalid child ###
            else:
                raise ValueError('Invalid child with name: %s'
                                 % child.plotly_name)

    def _init_props(self):
        """
        Ensure that this object's properties dict has been initialized. When
        the object has a parent, this ensures that the parent has an
        initialized properties dict with this object's plotly_name as a key.

        Returns
        -------
        None
        """
        # Ensure that _data is initialized.
        if self._props is not None:
            pass
        else:
            self._parent._init_child_props(self)

    def _init_child_props(self, child):
        """
        Ensure that a properties dict has been initialized for a child object

        Parameters
        ----------
        child : BasePlotlyType

        Returns
        -------
        None
        """
        # Init our own properties
        # -----------------------
        self._init_props()

        # Child a compound property
        # -------------------------
        if child.plotly_name in self._compound_props:
            if child.plotly_name not in self._props:
                self._props[child.plotly_name] = {}

        # Child an element of a compound array property
        # ---------------------------------------------
        elif child.plotly_name in self._compound_array_props:
            children = self._compound_array_props[child.plotly_name]
            child_ind = BaseFigure._index_is(children, child)
            assert child_ind is not None

            if child.plotly_name not in self._props:
                # Initialize list
                self._props[child.plotly_name] = []

            # Make sure list is long enough for child
            children_list = self._props[child.plotly_name]
            while len(children_list) <= child_ind:
                children_list.append({})

        # Invalid child
        # -------------
        else:
            raise ValueError('Invalid child with name: %s'
                             % child.plotly_name)

    def _get_child_prop_defaults(self, child):
        """
        Return default properties dict for child

        Parameters
        ----------
        child : BasePlotlyType

        Returns
        -------
        dict
        """
        if self._prop_defaults is None:
            # If this node's default properties are uninitialized then so are
            # its child's
            return None
        else:
            # ### Child a compound property ###
            if child.plotly_name in self._compound_props:
                return self._prop_defaults.get(child.plotly_name, None)

            # ### Child an element of a compound array property ###
            elif child.plotly_name in self._compound_array_props:
                children = self._compound_array_props[child.plotly_name]
                child_ind = BaseFigure._index_is(children, child)

                assert child_ind is not None

                children_props = self._prop_defaults.get(
                    child.plotly_name, None)

                return (children_props[child_ind]
                        if children_props is not None and
                        len(children_props) > child_ind
                        else None)

            # ### Invalid child ###
            else:
                raise ValueError('Invalid child with name: %s'
                                 % child.plotly_name)

    @property
    def _prop_defaults(self):
        """
        Return default properties dict

        Returns
        -------
        dict
        """
        if self.parent is None:
            return None
        else:
            return self.parent._get_child_prop_defaults(self)

    @property
    def parent(self):
        """
        Return the object's parent, or None if the object has no parent
        Returns
        -------
        BasePlotlyType|BaseFigure
        """
        return self._parent

    @property
    def figure(self):
        """
        Reference to the top-level Figure or FigureWidget that this object
        belongs to. None if the object does not belong to a Figure

        Returns
        -------
        Union[BaseFigure, None]
        """
        top_parent = self
        while top_parent is not None:
            if isinstance(top_parent, BaseFigure):
                break
            else:
                top_parent = top_parent.parent

        return top_parent

    # Magic Methods
    # -------------
    def __getitem__(self, prop):
        """
        Get item or nested item from object

        Parameters
        ----------
        prop : str|tuple

            If prop is the name of a property of this object, then the
            property is returned.

            If prop is a nested property path string (e.g. 'foo[1].bar'),
            then a nested property is returned (e.g. obj['foo'][1]['bar'])

            If prop is a path tuple (e.g. ('foo', 1, 'bar')), then a nested
            property is returned (e.g. obj['foo'][1]['bar']).

        Returns
        -------
        Any
        """

        # Normalize prop
        # --------------
        # Convert into a property tuple
        prop = BaseFigure._str_to_dict_path(prop)

        # Handle scalar case
        # ------------------
        # e.g. ('foo',)
        if len(prop) == 1:
            # Unwrap scalar tuple
            prop = prop[0]
            if prop not in self._validators:
                raise KeyError(prop)

            validator = self._validators[prop]
            if prop in self._compound_props:
                return validator.present(self._compound_props[prop])
            elif prop in self._compound_array_props:
                return validator.present(self._compound_array_props[prop])
            elif self._props is not None and prop in self._props:
                return validator.present(self._props[prop])
            elif self._prop_defaults is not None:
                return validator.present(self._prop_defaults.get(prop, None))
            else:
                return None

        # Handle non-scalar case
        # ----------------------
        # e.g. ('foo', 1), ()
        else:
            res = self
            for p in prop:
                res = res[p]

            return res

    def __contains__(self, prop):
        """
        Determine whether object contains a property or nested property

        Parameters
        ----------
        prop : str|tuple
            If prop is a simple string (e.g. 'foo'), then return true of the
            object contains an element named 'foo'

            If prop is a property path string (e.g. 'foo[0].bar'),
            then return true if the obejct contains the nested elements for
            each entry in the path string (e.g. 'bar' in obj['foo'][0])

            If prop is a property path tuple (e.g. ('foo', 0, 'bar')),
            then return true if the object contains the nested elements for
            each entry in the path string (e.g. 'bar' in obj['foo'][0])

        Returns
        -------
        bool
        """
        prop_tuple = BaseFigure._str_to_dict_path(prop)

        obj = self
        for p in prop_tuple:
            if isinstance(p, int):
                if isinstance(obj, tuple) and 0 <= p < len(obj):
                    obj = obj[p]
                else:
                    return False
            else:
                if p in obj._validators:
                    obj = obj[p]
                else:
                    return False

        return True

    def __setitem__(self, prop, value):
        """
        Parameters
        ----------
        prop : str
            The name of a direct child of this object

            Note: Setting nested properties using property path string or
            property path tuples is not supported.
        value
            New property value

        Returns
        -------
        None
        """

        # Normalize prop
        # --------------
        # Convert into a property tuple
        orig_prop = prop
        prop = BaseFigure._str_to_dict_path(prop)

        # Handle empty case
        # -----------------
        if len(prop) == 0:
            raise KeyError(orig_prop)

        # Handle scalar case
        # ------------------
        # e.g. ('foo',)
        if len(prop) == 1:

            # ### Unwrap scalar tuple ###
            prop = prop[0]

            # ### Validate prop ###
            if prop not in self._validators:
                self._raise_on_invalid_property_error(prop)

            # ### Get validator for this property ###
            validator = self._validators[prop]

            # ### Handle compound property ###
            if isinstance(validator, CompoundValidator):
                self._set_compound_prop(prop, value)

            # ### Handle compound array property ###
            elif isinstance(validator,
                            (CompoundArrayValidator, BaseDataValidator)):
                self._set_array_prop(prop, value)

            # ### Handle simple property ###
            else:
                self._set_prop(prop, value)

        # Handle non-scalar case
        # ----------------------
        # e.g. ('foo', 1), ()
        else:
            res = self
            for p in prop[:-1]:
                res = res[p]

            res[prop[-1]] = value

    def __setattr__(self, prop, value):
        """
        Parameters
        ----------
        prop : str
            The name of a direct child of this object
        value
            New property value
        Returns
        -------
        None
        """
        if (prop.startswith('_') or
                hasattr(self, prop) or
                prop in self._validators):
            # Let known properties and private properties through
            super().__setattr__(prop, value)
        else:
            # Raise error on unknown public properties
            self._raise_on_invalid_property_error(prop)

    def __iter__(self):
        """
        Return an iterator over the object's properties
        """
        return iter(self._validators.keys())

    def __eq__(self, other):
        """
        Test for equality

        To be considered equal, `other` must have the same type as this object
        and their `to_plotly_json` representaitons must be identical.

        Parameters
        ----------
        other
            The object to compare against

        Returns
        -------
        bool
        """
        if not isinstance(other, self.__class__):
            # Require objects to be of the same plotly type
            return False
        else:
            # Compare plotly_json representations

            # Use _vals_equal instead of `==` to handle cases where
            # underlying dicts contain numpy arrays
            return BasePlotlyType._vals_equal(
                self._props if self._props is not None else {},
                other._props if other._props is not None else {})

    @staticmethod
    def _build_repr_for_class(props, class_name, parent_path_str=None):
        """
        Helper to build representation string for a class

        Parameters
        ----------
        class_name : str
            Name of the class being represented
        parent_path_str : str of None (default)
            Name of the class's parent package to display
        props : dict
            Properties to unpack into the constructor

        Returns
        -------
        str
            The representation string
        """
        if parent_path_str:
            class_name = parent_path_str + '.' + class_name

        if len(props) == 0:
            repr_str = class_name + '()'
        else:
            pprinter = ElidedPrettyPrinter(threshold=200, width=120)
            pprint_res = pprinter.pformat(props)

            # pprint_res is indented by 1 space. Add extra 3 spaces for PEP8
            # complaint indent
            body = '   ' + pprint_res[1:-1].replace('\n', '\n   ')

            repr_str = class_name + '(**{\n ' + body + '\n})'

        return repr_str

    def __repr__(self):
        """
        Customize object representation when displayed in the
        terminal/notebook
        """
        repr_str = BasePlotlyType._build_repr_for_class(
            props=self._props if self._props is not None else {},
            class_name=self.__class__.__name__,
            parent_path_str=self._parent_path_str)

        return repr_str

    def _raise_on_invalid_property_error(self, *args):
        """
        Raise informative exception when invalid property names are
        encountered

        Parameters
        ----------
        args : list[str]
            List of property names that have already been determined to be
            invalid

        Raises
        ------
        ValueError
            Always
        """
        invalid_props = args
        if invalid_props:
            if len(invalid_props) == 1:
                prop_str = 'property'
                invalid_str = repr(invalid_props[0])
            else:
                prop_str = 'properties'
                invalid_str = repr(invalid_props)

            module_root = 'plotly.graph_objs.'
            if self._parent_path_str:
                full_obj_name = (module_root + self._parent_path_str + '.' +
                                 self.__class__.__name__)
            else:
                full_obj_name = module_root + self.__class__.__name__

            raise ValueError("Invalid {prop_str} specified for object of type "
                             "{full_obj_name}: {invalid_str}\n\n"
                             "    Valid properties:\n"
                             "{prop_descriptions}".format(
                                 prop_str=prop_str,
                                 full_obj_name=full_obj_name,
                                 invalid_str=invalid_str,
                                 prop_descriptions=self._prop_descriptions))

    def update(self, dict1=None, **kwargs):
        """
        Update the properties of an object with a dict and/or with
        keyword arguments.

        This recursively updates the structure of the original
        object with the values in the input dict / keyword arguments.

        Parameters
        ----------
        dict1 : dict
            Dictionary of properties to be updated
        kwargs :
            Keyword/value pair of properties to be updated

        Returns
        -------
        BasePlotlyType
            Updated plotly object
        """
        if self.figure:
            with self.figure.batch_update():
                BaseFigure._perform_update(self, dict1)
                BaseFigure._perform_update(self, kwargs)
        else:
            BaseFigure._perform_update(self, dict1)
            BaseFigure._perform_update(self, kwargs)

    @property
    def _in_batch_mode(self):
        """
        True if the object belongs to a figure that is currently in batch mode
        Returns
        -------
        bool
        """
        return self.parent and self.parent._in_batch_mode

    def _set_prop(self, prop, val):
        """
        Set the value of a simple property

        Parameters
        ----------
        prop : str
            Name of a simple (non-compound, non-array) property
        val
            The new property value

        Returns
        -------
        Any
            The coerced assigned value
        """

        # val is Undefined
        # ----------------
        if val is Undefined:
            # Do nothing
            return

        # Import value
        # ------------
        validator = self._validators.get(prop)
        val = validator.validate_coerce(val)

        # val is None
        # -----------
        if val is None:
            # Check if we should send null update
            if self._props and prop in self._props:
                # Remove property if not in batch mode
                if not self._in_batch_mode:
                    self._props.pop(prop)

                # Send property update message
                self._send_prop_set(prop, val)

        # val is valid value
        # ------------------
        else:
            # Make sure properties dict is initialized
            self._init_props()

            # Check whether the value is a change
            if (prop not in self._props or
                    not BasePlotlyType._vals_equal(self._props[prop], val)):
                # Set property value if not in batch mode
                if not self._in_batch_mode:
                    self._props[prop] = val

                # Send property update message
                self._send_prop_set(prop, val)

        return val

    def _set_compound_prop(self, prop, val):
        """
        Set the value of a compound property

        Parameters
        ----------
        prop : str
            Name of a compound property
        val
            The new property value

        Returns
        -------
        BasePlotlyType
            The coerced assigned object
        """

        # val is Undefined
        # ----------------
        if val is Undefined:
            # Do nothing
            return

        # Import value
        # ------------
        validator = self._validators.get(prop)
        # type: BasePlotlyType
        val = validator.validate_coerce(val)

        # Save deep copies of current and new states
        # ------------------------------------------
        curr_val = self._compound_props.get(prop, None)
        if curr_val is not None:
            curr_dict_val = deepcopy(curr_val._props)
        else:
            curr_dict_val = None

        if val is not None:
            new_dict_val = deepcopy(val._props)
        else:
            new_dict_val = None

        # Update _props dict
        # ------------------
        if not self._in_batch_mode:
            if not new_dict_val:
                if prop in self._props:
                    self._props.pop(prop)
            else:
                self._init_props()
                self._props[prop] = new_dict_val

        # Send update if there was a change in value
        # ------------------------------------------
        if not BasePlotlyType._vals_equal(curr_dict_val, new_dict_val):
            self._send_prop_set(prop, new_dict_val)

        # Reparent
        # --------
        # ### Reparent new value and clear orphan data ###
        val._parent = self
        val._orphan_props.clear()

        # ### Unparent old value and update orphan data ###
        if curr_val is not None:
            if curr_dict_val is not None:
                curr_val._orphan_props.update(curr_dict_val)
            curr_val._parent = None

        # Update _compound_props
        # ----------------------
        self._compound_props[prop] = val
        return val

    def _set_array_prop(self, prop, val):
        """
        Set the value of a compound property

        Parameters
        ----------
        prop : str
            Name of a compound property
        val
            The new property value

        Returns
        -------
        tuple[BasePlotlyType]
            The coerced assigned object
        """

        # val is Undefined
        # ----------------
        if val is Undefined:
            # Do nothing
            return

        # Import value
        # ------------
        validator = self._validators.get(prop)
        # type: Tuple[BasePlotlyType]
        val = validator.validate_coerce(val)

        # Save deep copies of current and new states
        # ------------------------------------------
        curr_val = self._compound_array_props.get(prop, None)
        if curr_val is not None:
            curr_dict_vals = [deepcopy(cv._props) for cv in curr_val]
        else:
            curr_dict_vals = None

        if val is not None:
            new_dict_vals = [deepcopy(nv._props) for nv in val]
        else:
            new_dict_vals = None

        # Update _props dict
        # ------------------
        if not self._in_batch_mode:
            if not new_dict_vals:
                if prop in self._props:
                    self._props.pop(prop)
            else:
                self._init_props()
                self._props[prop] = new_dict_vals

        # Send update if there was a change in value
        # ------------------------------------------
        if not BasePlotlyType._vals_equal(curr_dict_vals, new_dict_vals):
            self._send_prop_set(prop, new_dict_vals)

        # Reparent
        # --------
        # ### Reparent new values and clear orphan data ###
        if val is not None:
            for v in val:
                v._orphan_props.clear()
                v._parent = self

        # ### Unparent old value and update orphan data ###
        if curr_val is not None:
            for cv, cv_dict in zip(curr_val, curr_dict_vals):
                if cv_dict is not None:
                    cv._orphan_props.update(cv_dict)
                cv._parent = None

        # Update _compound_array_props
        # ----------------------------
        self._compound_array_props[prop] = val
        return val

    def _send_prop_set(self, prop_path_str, val):
        """
        Notify parent that a property has been set to a new value

        Parameters
        ----------
        prop_path_str : str
            Property path string (e.g. 'foo[0].bar') of property that
            was set, relative to this object
        val
            New value for property. Either a simple value, a dict,
            or a tuple of dicts. This should *not* be a BasePlotlyType object.

        Returns
        -------
        None
        """
        raise NotImplementedError()

    def _prop_set_child(self, child, prop_path_str, val):
        """
        Propagate property setting notification from child to parent

        Parameters
        ----------
        child : BasePlotlyType
            Child object
        prop_path_str : str
            Property path string (e.g. 'foo[0].bar') of property that
            was set, relative to `child`
        val
            New value for property. Either a simple value, a dict,
            or a tuple of dicts. This should *not* be a BasePlotlyType object.

        Returns
        -------
        None
        """

        # Child is compound array property
        # --------------------------------
        child_prop_val = getattr(self, child.plotly_name)
        if isinstance(child_prop_val, (list, tuple)):
            child_ind = BaseFigure._index_is(child_prop_val, child)
            obj_path = '{child_name}.{child_ind}.{prop}'.format(
                child_name=child.plotly_name,
                child_ind=child_ind,
                prop=prop_path_str)

        # Child is compound property
        # --------------------------
        else:
            obj_path = '{child_name}.{prop}'.format(
                child_name=child.plotly_name,
                prop=prop_path_str)

        # Propagate to parent
        # -------------------
        self._send_prop_set(obj_path, val)

    def _restyle_child(self, child, prop, val):
        """
        Propagate _restyle_child to parent

        Note: This method must match the name and signature of the
        corresponding method on BaseFigure
        """
        self._prop_set_child(child, prop, val)

    def _relayout_child(self, child, prop, val):
        """
        Propagate _relayout_child to parent

        Note: This method must match the name and signature of the
        corresponding method on BaseFigure
        """
        self._prop_set_child(child, prop, val)

    # Callbacks
    # ---------
    def _dispatch_change_callbacks(self, changed_paths):
        """
        Execute the appropriate change callback functions given a set of
        changed property path tuples

        Parameters
        ----------
        changed_paths : set[tuple[int|str]]

        Returns
        -------
        None
        """
        # Loop over registered callbacks
        # ------------------------------
        for prop_path_tuples, callbacks in self._change_callbacks.items():
            # ### Compute callback paths that changed ###
            common_paths = changed_paths.intersection(set(prop_path_tuples))
            if common_paths:

                # #### Invoke callback ####
                callback_args = [self[cb_path]
                                 for cb_path in prop_path_tuples]

                for callback in callbacks:
                    callback(self, *callback_args)

    def on_change(self, callback, *args, append=False):
        """
        Register callback function to be called when certain properties or
        subproperties of this object are modified.

        Callback will be invoked whenever ANY of these properties is
        modified. Furthermore. The callback will only be invoked once even
        if multiple properties are modified during the same restyle /
        relayout / update operation.

        Parameters
        ----------
        callback : function
            Function that accepts 1 + len(`args`) parameters. First parameter
            is this object. Second through last parameters are the
            property / subpropery values referenced by args.
        args : list[str|tuple[int|str]]
            List of property references where each reference may be one of:

              1) A property name string (e.g. 'foo') for direct properties
              2) A property path string (e.g. 'foo[0].bar') for
                 subproperties
              3) A property path tuple (e.g. ('foo', 0, 'bar')) for
                 subproperties

        append : bool
            True if callback should be appended to previously registered
            callback on the same properties, False if callback should replace
            previously registered callbacks on the same properties. Defaults
            to False.

        Examples
        --------

        Register callback that prints out the range extents of the xaxis and
        yaxis whenever either either of them changes.

        >>> fig.layout.on_change(
        ...   lambda obj, xrange, yrange: print("%s-%s" % (xrange, yrange)),
        ...   ('xaxis', 'range'), ('yaxis', 'range'))


        Returns
        -------
        None
        """

        # Warn if object not descendent of a figure
        # -----------------------------------------
        if not self.figure:
            class_name = self.__class__.__name__
            msg = """
{class_name} object is not a descendant of a Figure.
on_change callbacks are not supported in this case.
""".format(class_name=class_name)
            raise ValueError(msg)

        # Validate args not empty
        # -----------------------
        if len(args) == 0:
            raise ValueError(
                'At least one change property must be specified')

        # Validate args
        # -------------
        invalid_args = [arg for arg in args if arg not in self]
        if invalid_args:
            raise ValueError(
                'Invalid property specification(s): %s' % invalid_args)

        # Normalize args to path tuples
        # -----------------------------
        arg_tuples = tuple([BaseFigure._str_to_dict_path(a) for a in args])

        # Initialize callbacks list
        # -------------------------
        # Initialize an empty callbacks list if there are no previously
        # defined callbacks for this collection of args, or if append is False
        if arg_tuples not in self._change_callbacks or not append:
            self._change_callbacks[arg_tuples] = []

        # Register callback
        # -----------------
        self._change_callbacks[arg_tuples].append(callback)

    def to_plotly_json(self):
        """
        Return plotly JSON representation of object as a Python dict

        Returns
        -------
        dict
        """
        return deepcopy(self._props if self._props is not None else {})

    @staticmethod
    def _vals_equal(v1, v2):
        """
        Recursive equality function that handles nested dicts / tuples / lists
        that contain numpy arrays.

        v1
            First value to compare
        v2
            Second value to compare

        Returns
        -------
        bool
            True if v1 and v2 are equal, False otherwise
        """
        if (np is not None and
                (isinstance(v1, np.ndarray) or isinstance(v2, np.ndarray))):
            return np.array_equal(v1, v2)
        elif isinstance(v1, (list, tuple)):
            # Handle recursive equality on lists and tuples
            return (isinstance(v2, (list, tuple)) and
                    len(v1) == len(v2) and
                    all(BasePlotlyType._vals_equal(e1, e2)
                        for e1, e2 in zip(v1, v2)))
        elif isinstance(v1, dict):
            # Handle recursive equality on dicts
            return (isinstance(v2, dict) and
                    set(v1.keys()) == set(v2.keys()) and
                    all(BasePlotlyType._vals_equal(v1[k], v2[k]) for k in v1))
        else:
            return v1 == v2


class BaseLayoutHierarchyType(BasePlotlyType):
    """
    Base class for all types in the layout hierarchy
    """

    @property
    def _parent_path_str(self) -> str:
        pass

    def __init__(self, plotly_name, **kwargs):
        super().__init__(plotly_name, **kwargs)

    def _send_prop_set(self, prop_path_str, val):
        if self.parent:
            # ### Inform parent of relayout operation ###
            self.parent._relayout_child(self, prop_path_str, val)


class BaseLayoutType(BaseLayoutHierarchyType):
    """
    Base class for the layout type. The Layout class itself is a
    code-generated subclass.
    """

    # Dynamic properties
    # ------------------
    # Unlike all other plotly types, BaseLayoutType has dynamic properties.
    # These are used when a layout has multiple instances of subplot types
    # (xaxis2, yaxis3, geo4, etc.)
    #
    # The base version of each suplot type is defined in the schema and code
    # generated. So the Layout subclass has statically defined properties
    # for xaxis, yaxis, geo, ternary, and scene. But, we need to dynamically
    # generated properties/validators as needed for xaxis2, yaxis3, etc.

    # # ### Create subplot property regular expression ###
    _subplotid_prop_names = ['xaxis', 'yaxis', 'geo', 'ternary', 'scene']
    _subplotid_prop_re = re.compile(
        '(' + '|'.join(_subplotid_prop_names) + ')(\d+)')

    @property
    def _subplotid_validators(self):
        """
        dict of validator classes for each subplot type

        Returns
        -------
        dict
        """
        from plotly.validators.layout import (XAxisValidator, YAxisValidator,
                                              GeoValidator, TernaryValidator,
                                              SceneValidator)

        return {
            'xaxis': XAxisValidator,
            'yaxis': YAxisValidator,
            'geo': GeoValidator,
            'ternary': TernaryValidator,
            'scene': SceneValidator
        }

    def __init__(self, plotly_name, **kwargs):
        """
        Construct a new BaseLayoutType object

        Parameters
        ----------
        plotly_name : str
            Name of the object (should always be 'layout')
        kwargs : dict[str, any]
            Properties that were not recognized by the Layout subclass.
            These are subplot identifiers (xaxis2, geo4, etc.) or they are
            invalid properties.
        """
        # Validate inputs
        # ---------------
        assert plotly_name == 'layout'

        # Call superclass constructor
        # ---------------------------
        super().__init__(plotly_name)

        # Initialize _subplotid_props
        # ---------------------------
        # This is a set storing the names of the layout's dynamic subplot
        # properties
        self._subplotid_props = set()

        # Process kwargs
        # --------------
        self._process_kwargs(**kwargs)

    def _process_kwargs(self, **kwargs):
        """
        Process any extra kwargs that are not predefined as constructor params
        """
        unknown_kwargs = {
            k: v
            for k, v in kwargs.items()
            if not self._subplotid_prop_re.fullmatch(k)
        }
        super()._process_kwargs(**unknown_kwargs)

        subplot_kwargs = {
            k: v
            for k, v in kwargs.items()
            if self._subplotid_prop_re.fullmatch(k)
        }

        for prop, value in subplot_kwargs.items():
            self._set_subplotid_prop(prop, value)

    def _set_subplotid_prop(self, prop, value):
        """
        Set a subplot property on the layout

        Parameters
        ----------
        prop : str
            A valid subplot property
        value
            Subplot value
        """
        # Get regular expression match
        # ----------------------------
        # Note: we already tested that match exists in the constructor
        match = self._subplotid_prop_re.fullmatch(prop)
        subplot_prop = match.group(1)
        suffix_digit = int(match.group(2))

        # Validate suffix digit
        # ---------------------
        if suffix_digit == 0:
            raise TypeError('Subplot properties may only be suffixed by an '
                            'integer >= 1\n'
                            'Received {k}'.format(k=prop))

        # Handle suffix_digit == 1
        # ------------------------
        # In this case we remove suffix digit (e.g. xaxis1 -> xaxis)
        if suffix_digit == 1:
            prop = subplot_prop

        # Construct and add validator
        # ---------------------------
        if prop not in self._validators:
            validator_class = self._subplotid_validators[subplot_prop]
            validator = validator_class(plotly_name=prop)
            self._validators[prop] = validator

        # Import value
        # ------------
        # Use the standard _set_compound_prop method to
        # validate/coerce/import subplot value. This must be called AFTER
        # the validator instance is added to self._validators above.
        self._set_compound_prop(prop, value)
        self._subplotid_props.add(prop)

    def _strip_subplot_suffix_of_1(self, prop):
        """
        Strip the suffix for subplot property names that have a suffix of 1.
        All other properties are returned unchanged

        e.g. 'xaxis1' -> 'xaxis'

        Parameters
        ----------
        prop : str|tuple

        Returns
        -------
        str|tuple
        """
        # Let parent handle non-scalar cases
        # ----------------------------------
        # e.g. ('xaxis', 'range') or 'xaxis.range'
        prop_tuple = BaseFigure._str_to_dict_path(prop)
        if (len(prop_tuple) != 1 or
                not isinstance(prop_tuple[0], str)):
            return prop
        else:
            # Unwrap to scalar string
            prop = prop_tuple[0]

        # Handle subplot suffix digit of 1
        # --------------------------------
        # Remove digit of 1 from subplot id (e.g.. xaxis1 -> xaxis)
        match = self._subplotid_prop_re.fullmatch(prop)

        if match:
            subplot_prop = match.group(1)
            suffix_digit = int(match.group(2))
            if subplot_prop and suffix_digit == 1:
                prop = subplot_prop

        return prop

    def __getattr__(self, prop):
        """
        Custom __getattr__ that handles dynamic subplot properties
        """
        prop = self._strip_subplot_suffix_of_1(prop)
        if prop in self._subplotid_props:
            validator = self._validators[prop]
            return validator.present(self._compound_props[prop])
        else:
            return super().__getattribute__(prop)

    def __getitem__(self, prop):
        """
        Custom __getitem__ that handles dynamic subplot properties
        """
        prop = self._strip_subplot_suffix_of_1(prop)
        return super().__getitem__(prop)

    def __contains__(self, prop):
        """
        Custom __contains__ that handles dynamic subplot properties
        """
        prop = self._strip_subplot_suffix_of_1(prop)
        return super().__contains__(prop)

    def __setitem__(self, prop, value):
        """
        Custom __setitem__ that handles dynamic subplot properties
        """
        # Convert prop to prop tuple
        # --------------------------
        prop_tuple = BaseFigure._str_to_dict_path(prop)
        if len(prop_tuple) != 1 or not isinstance(prop_tuple[0], str):
            # Let parent handle non-scalar non-string cases
            super().__setitem__(prop, value)
            return
        else:
            # Unwrap prop tuple
            prop = prop_tuple[0]

        # Check for subplot assignment
        # ----------------------------
        match = self._subplotid_prop_re.fullmatch(prop)
        if match is None:
            # Set as ordinary property
            super().__setitem__(prop, value)
        else:
            # Set as subplotid property
            self._set_subplotid_prop(prop, value)

    def __setattr__(self, prop, value):
        """
        Custom __setattr__ that handles dynamic subplot properties
        """
        # Check for subplot assignment
        # ----------------------------
        match = self._subplotid_prop_re.fullmatch(prop)
        if match is None:
            # Set as ordinary property
            super().__setattr__(prop, value)
        else:
            # Set as subplotid property
            self._set_subplotid_prop(prop, value)

    def __dir__(self):
        """
        Custom __dir__ that handles dynamic subplot properties
        """
        # Include any active subplot values
        return list(super().__dir__()) + sorted(self._subplotid_props)


class BaseTraceHierarchyType(BasePlotlyType):
    """
    Base class for all types in the trace hierarchy
    """

    def __init__(self, plotly_name, **kwargs):
        super().__init__(plotly_name, **kwargs)

    def _send_prop_set(self, prop_path_str, val):
        if self.parent:
            # ### Inform parent of restyle operation ###
            self.parent._restyle_child(self, prop_path_str, val)


class BaseTraceType(BaseTraceHierarchyType):
    """
    Base class for the all trace types.

    Specific trace type classes (Scatter, Bar, etc.) are code generated as
    subclasses of this class.
    """

    def __init__(self, plotly_name, **kwargs):
        super().__init__(plotly_name, **kwargs)

        # Initialize callback function lists
        # ----------------------------------
        # ### Callbacks to be called on hover ###
        self._hover_callbacks = []

        # ### Callbacks to be called on unhover ###
        self._unhover_callbacks = []

        # ### Callbacks to be called on click ###
        self._click_callbacks = []

        # ### Callbacks to be called on selection ###
        self._select_callbacks = []

    # uid
    # ---
    # All trace types must have a top-level UID
    @property
    def uid(self) -> str:
        raise NotImplementedError

    @uid.setter
    def uid(self, val):
        raise NotImplementedError

    # Hover
    # -----
    def on_hover(self,
                 callback: typ.Callable[
                     ['BaseTraceType', Points, InputDeviceState], None],
                 append=False):
        """
        Register function to be called when the user hovers over one or more
        points in this trace

        Note: Callbacks will only be triggered when the trace belongs to a
        instance of plotly.graph_objs.FigureWidget and it is displayed in an
        ipywidget context. Callbacks will not be triggered when on figures
        that are displayed using plot/iplot.

        Parameters
        ----------
        callback
            Callable function that accepts 3 arguments

            - this trace
            - plotly.callbacks.Points object
            - plotly.callbacks.InputDeviceState object

        append : bool
            If False (the default), this callback replaces any previously
            defined on_hover callbacks for this trace. If True,
            this callback is appended to the list of any previously defined
            callbacks.

        Returns
        -------
        None
        """
        if not append:
            self._hover_callbacks.clear()

        if callback:
            self._hover_callbacks.append(callback)

    def _dispatch_on_hover(self, points: Points, state: InputDeviceState):
        """
        Dispatch points and device state all all hover callbacks
        """
        for callback in self._hover_callbacks:
            callback(self, points, state)

    # Unhover
    # -------
    def on_unhover(self,
                   callback: typ.Callable[
                       ['BaseTraceType', Points, InputDeviceState], None],
                   append=False):
        """
        Register function to be called when the user unhovers away from one
        or more points in this trace.

        Note: Callbacks will only be triggered when the trace belongs to a
        instance of plotly.graph_objs.FigureWidget and it is displayed in an
        ipywidget context. Callbacks will not be triggered when on figures
        that are displayed using plot/iplot.

        Parameters
        ----------
        callback
            Callable function that accepts 3 arguments

            - this trace
            - plotly.callbacks.Points object
            - plotly.callbacks.InputDeviceState object

        append : bool
            If False (the default), this callback replaces any previously
            defined on_unhover callbacks for this trace. If True,
            this callback is appended to the list of any previously defined
            callbacks.

        Returns
        -------
        None
        """
        if not append:
            self._unhover_callbacks.clear()

        if callback:
            self._unhover_callbacks.append(callback)

    def _dispatch_on_unhover(self, points: Points, state: InputDeviceState):
        """
        Dispatch points and device state all all hover callbacks
        """
        for callback in self._unhover_callbacks:
            callback(self, points, state)

    # Click
    # -----
    def on_click(self,
                 callback: typ.Callable[
                     ['BaseTraceType', Points, InputDeviceState], None],
                 append=False):
        """
        Register function to be called when the user clicks on one or more
        points in this trace.

        Note: Callbacks will only be triggered when the trace belongs to a
        instance of plotly.graph_objs.FigureWidget and it is displayed in an
        ipywidget context. Callbacks will not be triggered when on figures
        that are displayed using plot/iplot.

        Parameters
        ----------
        callback
            Callable function that accepts 3 arguments

            - this trace
            - plotly.callbacks.Points object
            - plotly.callbacks.InputDeviceState object

        append : bool
            If False (the default), this callback replaces any previously
            defined on_click callbacks for this trace. If True,
            this callback is appended to the list of any previously defined
            callbacks.

        Returns
        -------
        None
        """
        if not append:
            self._click_callbacks.clear()
        if callback:
            self._click_callbacks.append(callback)

    def _dispatch_on_click(self, points: Points, state: InputDeviceState):
        """
        Dispatch points and device state all all hover callbacks
        """
        for callback in self._click_callbacks:
            callback(self, points, state)

    # Select
    # ------
    def on_selection(
            self,
            callback: typ.Callable[[
                'BaseTraceType', Points, typ.Union[BoxSelector, LassoSelector]
            ], None],
            append=False):
        """
        Register function to be called when the user selects one or more
        points in this trace.

        Note: Callbacks will only be triggered when the trace belongs to a
        instance of plotly.graph_objs.FigureWidget and it is displayed in an
        ipywidget context. Callbacks will not be triggered when on figures
        that are displayed using plot/iplot.

        Parameters
        ----------
        callback
            Callable function that accepts 4 arguments

            - this trace
            - plotly.callbacks.Points object
            - plotly.callbacks.BoxSelector or plotly.callbacks.LassoSelector

        append : bool
            If False (the default), this callback replaces any previously
            defined on_selection callbacks for this trace. If True,
            this callback is appended to the list of any previously defined
            callbacks.

        Returns
        -------
        None
        """
        if not append:
            self._select_callbacks.clear()

        if callback:
            self._select_callbacks.append(callback)

    def _dispatch_on_selection(self,
                               points: Points,
                               selector: typ.Union[BoxSelector, LassoSelector]):
        """
        Dispatch points and selector info to selection callbacks
        """
        for callback in self._select_callbacks:
            callback(self, points, selector)


class BaseFrameHierarchyType(BasePlotlyType):
    """
    Base class for all types in the trace hierarchy
    """

    def __init__(self, plotly_name, **kwargs):
        super().__init__(plotly_name, **kwargs)

    def _send_prop_set(self, prop_path_str, val):
        # Note: Frames are not supported by FigureWidget, and updates are not
        # propagated to parents
        pass

    def on_change(self, callback, *args):
        raise NotImplementedError(
            'Change callbacks are not supported on Frames')