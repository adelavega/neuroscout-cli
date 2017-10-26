from nipype.pipeline.engine import Workflow, Node
import nipype.algorithms.modelgen as model
from nipype.interfaces.io import DataSink
from nipype.interfaces.utility import Function, IdentityInterface
from nipype.workflows.fmri.fsl import (create_modelfit_workflow,
                                       create_fixed_effects_flow)

import os


def create_first_level(bids_dir, work_dir, task, subjects, runs, contrasts,
                       event_data,
                       config=None,
                       out_dir=None,
                       TR=2):
    """
    Set up workflow
    """
    wf = Workflow(name='first_level')
    wf.base_dir = work_dir

    """
    Subject iterator
    """
    infosource = Node(IdentityInterface(fields=['subject_id']),
                      name="infosource")
    infosource.iterables = ('subject_id', subjects)

    """"
    Data source
    """

    def get_data(bids_dir, subject_id, event_data, runs):
        import os
        from nipype.interfaces.base import Bunch
        # Convert design matrix to bunches
        subject_bunches = []
        subject_data = event_data[event_data['subject'] == int(subject_id)]
        subject_runs = [int(r['number']) for r in runs if r['subject'] == subject_id]
        for r, run_data in subject_data.groupby('run'):
            if r in subject_runs:
                conds = []
                onsets = []
                durations = []
                amplitudes = []
                for c, cond_data in run_data.groupby('condition'):
                    cond_data = cond_data.reset_index()
                    conds.append(str(c))
                    onsets.append(cond_data['onset'].tolist())
                    durations.append(cond_data['duration'].tolist())
                    amplitudes.append(cond_data['amplitude'].tolist())
                bunch = Bunch(conditions=conds,
                              onsets=onsets,
                              durations=durations,
                              amplitudes=amplitudes)
                subject_bunches.append(bunch)

        func = [r['func_path'] for r in runs if r['subject'] == subject_id]
        func = [os.path.join(bids_dir, f) for f in func]
        bm = [r['mask_path'] for r in runs if r['subject'] == subject_id]
        bm = [os.path.join(bids_dir, b) for b in bm]
        bm = bm[0]  # DANGER DANGER (but gets it to work)

        return func, bm, subject_bunches

    datasource = Node(Function(input_names=['bids_dir', 'subject_id',
                                            'event_data', 'runs'],
                               output_names=['func', 'brainmask',
                                             'subject_info'],
                               function=get_data),
                      name='datasource')
    datasource.inputs.runs = runs
    datasource.inputs.bids_dir = bids_dir
    datasource.inputs.event_data = event_data
    wf.connect([((infosource, datasource, [('subject_id', 'subject_id')]))])

    """
    Specify fMRI model
    """

    modelspec = Node(interface=model.SpecifyModel(), name="modelspec")
    modelspec.inputs.input_units = 'secs'
    modelspec.inputs.time_repetition = TR
    modelspec.inputs.high_pass_filter_cutoff = 100.

    wf.connect(datasource, 'subject_info', modelspec, 'subject_info')
    wf.connect(datasource, 'func', modelspec, 'functional_runs')

    """
    Fit model to each run
    """

    modelfit = create_modelfit_workflow()

    modelfit.inputs.inputspec.contrasts = contrasts
    modelfit.inputs.inputspec.interscan_interval = TR
    modelfit.inputs.inputspec.model_serial_correlations = True
    modelfit.inputs.inputspec.bases = {'gamma': {'derivs': True}}

    wf.connect(modelspec, 'session_info', modelfit, 'inputspec.session_info')
    wf.connect(datasource, 'func', modelfit, 'inputspec.functional_data')

    """
    Fixed effects workflow to combine runs
    """

    fixed_fx = create_fixed_effects_flow()
    wf.connect(datasource, 'brainmask', fixed_fx, 'flameo.mask_file')

    def sort_copes(copes, varcopes, contrasts):
        import numpy as np
        if not isinstance(copes, list):
            copes = [copes]
            varcopes = [varcopes]
        num_copes = len(contrasts)
        n_runs = len(copes)
        all_copes = np.array(copes).flatten()
        all_varcopes = np.array(varcopes).flatten()
        outcopes = all_copes.reshape(int(len(all_copes) / num_copes),
                                     num_copes).T.tolist()
        outvarcopes = all_varcopes.reshape(int(len(all_varcopes) / num_copes),
                                           num_copes).T.tolist()
        return outcopes, outvarcopes, n_runs

    cope_sorter = Node(Function(input_names=['copes', 'varcopes',
                                             'contrasts'],
                                output_names=['copes', 'varcopes',
                                              'n_runs'],
                                function=sort_copes),
                       name='cope_sorter')
    cope_sorter.inputs.contrasts = contrasts

    wf.connect([(modelfit, cope_sorter, [('outputspec.copes', 'copes')]),
                (modelfit, cope_sorter, [('outputspec.varcopes', 'varcopes')]),
                (cope_sorter, fixed_fx, [('copes', 'inputspec.copes'),
                                         ('varcopes', 'inputspec.varcopes'),
                                         ('n_runs', 'l2model.num_copes')]),
                (modelfit, fixed_fx, [('outputspec.dof_file',
                                       'inputspec.dof_files'),
                                      ])
                ])
    """
    Save to datasink
    """

    def get_subs(subject_id, conds):
        """ Generate substitutions """
        subs = [('_subject_id_%s' % subject_id, '')]

        for i in range(len(conds)):
            subs.append(('_flameo%d/cope1.' % i, 'cope%02d.' % (i + 1)))
            subs.append(('_flameo%d/varcope1.' % i, 'varcope%02d.' % (i + 1)))
            subs.append(('_flameo%d/zstat1.' % i, 'zstat%02d.' % (i + 1)))
            subs.append(('_flameo%d/tstat1.' % i, 'tstat%02d.' % (i + 1)))
            subs.append(('_flameo%d/res4d.' % i, 'res4d%02d.' % (i + 1)))
            subs.append(('_warpall%d/cope1_warp.' % i,
                         'cope%02d.' % (i + 1)))
            subs.append(('_warpall%d/varcope1_warp.' % (len(conds) + i),
                         'varcope%02d.' % (i + 1)))
            subs.append(('_warpall%d/zstat1_warp.' % (2 * len(conds) + i),
                         'zstat%02d.' % (i + 1)))
            subs.append(('_warpall%d/cope1_trans.' % i,
                         'cope%02d.' % (i + 1)))
            subs.append(('_warpall%d/varcope1_trans.' % (len(conds) + i),
                         'varcope%02d.' % (i + 1)))
            subs.append(('_warpall%d/zstat1_trans.' % (2 * len(conds) + i),
                         'zstat%02d.' % (i + 1)))
        return subs

    subsgen = Node(Function(input_names=['subject_id', 'conds'],
                            output_names=['substitutions'],
                            function=get_subs),
                   name='subsgen')

    datasink = Node(DataSink(), name="datasink")
    if out_dir is not None:
        datasink.inputs.base_directory = os.path.abspath(out_dir)

    wf.connect(infosource, 'subject_id', datasink, 'container')
    wf.connect(infosource, 'subject_id', subsgen, 'subject_id')
    wf.connect(subsgen, 'substitutions', datasink, 'substitutions')
    subsgen.inputs.conds = contrasts

    wf.connect([(modelfit.get_node('modelgen'), datasink,
                 [('design_cov', 'qa.model'),
                  ('design_image', 'qa.model.@matrix_image'),
                  ('design_file', 'qa.model.@matrix'),
                  ])])

    wf.connect([(fixed_fx.get_node('outputspec'), datasink,
                 [('res4d', 'res4d'),
                  ('copes', 'copes'),
                  ('varcopes', 'varcopes'),
                  ('zstats', 'zstats'),
                  ('tstats', 'tstats')])
                ])
    return wf
