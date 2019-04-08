import _plotly_utils.basevalidators


class MinValidator(_plotly_utils.basevalidators.NumberValidator):

    def __init__(
        self, plotly_name='min', parent_name='layout.ternary.baxis', **kwargs
    ):
        super(MinValidator, self).__init__(
            plotly_name=plotly_name,
            parent_name=parent_name,
            edit_type=kwargs.pop('edit_type', 'plot'),
            min=kwargs.pop('min', 0),
            role=kwargs.pop('role', 'info'),
            **kwargs
        )