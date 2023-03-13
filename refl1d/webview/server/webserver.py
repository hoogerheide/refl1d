# from .main import setup_bumps

from dataclasses import dataclass
import itertools
import threading
import signal
from types import GeneratorType
from typing import Dict, List, Literal, Optional, Union, Tuple, TypedDict, cast
from datetime import datetime
import warnings
from queue import Queue
from collections import deque
from aiohttp import web, ClientSession
import numpy as np
import asyncio
import socketio
from pathlib import Path, PurePath
import json
from copy import deepcopy
from blinker import Signal
from uuid import uuid4

import mimetypes
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/html", ".html")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/svg+xml", ".svg")

from bumps.fitters import DreamFit, LevenbergMarquardtFit, SimplexFit, DEFit, MPFit, BFGSFit, FitDriver, fit, nllf_scale, format_uncertainty
from bumps.serialize import to_dict as serialize, from_dict as deserialize
from bumps.mapper import MPMapper
from bumps.parameter import Parameter, Variable, unique
import bumps.fitproblem
import bumps.plotutil
import bumps.dream.views, bumps.dream.varplot, bumps.dream.stats, bumps.dream.state
import bumps.errplot
import refl1d.errors
import refl1d.fitproblem, refl1d.probe
from refl1d.experiment import Experiment, MixedExperiment, ExperimentBase

# Register the refl1d model loader
import refl1d.fitplugin
import bumps.cli
bumps.cli.install_plugin(refl1d.fitplugin)

from .fit_thread import FitThread, EVT_FIT_COMPLETE, EVT_FIT_PROGRESS
from .profile_plot import plot_sld_profile_plotly
from .varplot import plot_vars
# from .state_hdf5_backed import State
from .state import State

# can get by name and not just by id
MODEL_EXT = '.json'

FITTERS = (DreamFit, LevenbergMarquardtFit, SimplexFit, DEFit, MPFit, BFGSFit)
FITTERS_BY_ID = dict([(fitter.id, fitter) for fitter in FITTERS])
print(FITTERS_BY_ID)
FITTER_DEFAULTS = {}
for fitter in FITTERS:
    FITTER_DEFAULTS[fitter.id] = {
        "name": fitter.name,
        "settings": dict(fitter.settings)
    }


routes = web.RouteTableDef()
# sio = socketio.AsyncServer(cors_allowed_origins="*", serializer='msgpack')
sio = socketio.AsyncServer(cors_allowed_origins="*")
app = web.Application()
client_path = Path(__file__).parent.parent / 'client'
index_path = client_path / 'dist'
static_assets_path = index_path / 'assets'

sio.attach(app)

TopicNameType = Literal[
    "log",
    "update_parameters",
    "update_model",
    "model_loaded",
    "fit_active",
    "uncertainty_update",
    "convergence_update",
    "fitter_settings",
    "fitter_active",
]


state = State()

def rest_get(fn):
    """
    Add a REST (GET) route for the function, which can also be used for 
    """
    @routes.get(f"/{fn.__name__}")
    async def handler(request: web.Request):
        result = await fn(**request.query)
        return web.json_response(result)
    
    # pass the function to the next decorator unchanged...
    return fn

async def index(request):
    """Serve the client-side application."""
    # check if the locally-build site has the correct version:
    with open(client_path / 'package.json', 'r') as package_json:
        client_version = json.load(package_json)['version'].strip()

    try:
        local_version = open(index_path / 'VERSION', 'rt').read().strip()
    except FileNotFoundError:
        local_version = None

    print(index_path, local_version, client_version, local_version == client_version)
    if client_version == local_version:
        return web.FileResponse(index_path / 'index.html')
    else:
        CDN = f"https://cdn.jsdelivr.net/npm/refl1d-webview-client@{client_version}/dist"
        with open(client_path / 'index_template.txt', 'r') as index_template:
            index_html = index_template.read().format(cdn=CDN)
        return web.Response(body=index_html, content_type="text/html")
    
@sio.event
async def connect(sid, environ, data=None):
    # re-send last message for all topics
    # now that panels are retrieving topics when they load, is this still
    # needed or useful?
    for topic, contents in state.topics.items():
        message = contents[-1] if len(contents) > 0 else None
        if message is not None:
            await sio.emit(topic, message, to=sid)
    print("connect ", sid)

@sio.event
async def load_problem_file(sid: str, pathlist: List[str], filename: str):    
    path = Path(*pathlist, filename)
    print(f'model loading: {path}')
    await log(f'model_loading: {path}')
    if filename.endswith(".json"):
        with open(path, "rt") as input_file:
            serialized = json.loads(input_file.read())
        problem = deserialize(serialized)
    else:
        from bumps.cli import load_model
        problem = load_model(str(path))
    assert isinstance(problem, bumps.fitproblem.FitProblem)
    # problem_state = ProblemState(problem, pathlist, filename)
    state.problem.filename = filename
    state.problem.pathlist = pathlist
    state.problem.fitProblem = problem
    print(f'model loaded: {path}')
    await log(f'model loaded: {path}')
    await publish("", "model_loaded", {"pathlist": pathlist, "filename": filename})
    await publish("", "update_model", True)
    await publish("", "update_parameters", True)

@sio.event
async def get_model_names(sid: str=""):
    problem = state.problem.fitProblem
    if problem is None:
        return None
    output: List[Dict] = []
    for model_index, model in enumerate(problem.models):
        if isinstance(model, Experiment):
            output.append(dict(name=model.name, part_name=None, model_index=model_index, part_index=0))
        elif isinstance(model, MixedExperiment):
            for part_index, part in enumerate(model.parts):
                output.append(dict(name=model.name, part_name=part.name, model_index=model_index, part_index=part_index))
    return output

@sio.event
async def save_problem_file(sid: str, pathlist: Optional[List[str]] = None, filename: Optional[str] = None, overwrite: bool = False):
    problem_state = state.problem
    if problem_state is None:
        print("Save failed: no problem loaded.")
        return
    if pathlist is None:
        pathlist = problem_state.pathlist
    if filename is None:
        filename = problem_state.filename

    if pathlist is None or filename is None:
        print("no filename and path provided to save")
        return

    path = Path(*pathlist)
    save_filename = Path(filename).stem + MODEL_EXT
    print({"path": path, "filename": save_filename})
    if not overwrite and Path.exists(path / save_filename):
        #confirmation needed:
        return save_filename

    serialized = serialize(problem_state.fitProblem)
    with open(Path(path, save_filename), "wt") as output_file:
        output_file.write(json.dumps(serialized))

    await log(f'Saved: {filename} at path: {path}')
    return False

@sio.event
async def start_fit(sid: str="", fitter_id: str="", kwargs=None):
    kwargs = {} if kwargs is None else kwargs
    problem_state = state.problem
    if problem_state is None:
        await log("Error: Can't start fit if no problem loaded")
    else:
        fitProblem = problem_state.fitProblem
        mapper = MPMapper.start_mapper(fitProblem, None, cpus=0)
        monitors = []
        fitclass = FITTERS_BY_ID[fitter_id]
        driver = FitDriver(fitclass=fitclass, mapper=mapper, problem=fitProblem, monitors=monitors, **kwargs)
        x, fx = driver.fit()
        driver.show()

@sio.event
async def stop_fit(sid: str = ""):
    abort_queue = state.abort_queue
    abort_queue.put_nowait(True)

def get_chisq(problem: refl1d.fitproblem.FitProblem, nllf=None):
    nllf = problem.nllf() if nllf is None else nllf
    scale, err = nllf_scale(problem)
    chisq = format_uncertainty(scale*nllf, err)
    return chisq

def get_num_steps(fitter_id: str, num_fitparams: int, options: Optional[Dict] = None):
    options = FITTER_DEFAULTS[fitter_id] if options is None else options
    steps = options['steps']
    if fitter_id == 'dream' and steps == 0:
        print('dream: ', options)
        total_pop = options['pop'] * num_fitparams
        print('total_pop: ', total_pop)
        sample_steps = int(options['samples'] / total_pop)
        print('sample_steps: ', sample_steps)
        print('steps: ', options['burn'] + sample_steps)
        return options['burn'] + sample_steps
    else:
        return steps

@sio.event
async def start_fit_thread(sid: str="", fitter_id: str="", options=None):
    options = {} if options is None else options    # session_id: str = app["active_session"]
    fitProblem = state.problem.fitProblem if state.problem is not None else None
    if fitProblem is None:
        await log("Error: Can't start fit if no problem loaded")
    else:
        fit_state = state.fitting
        fitclass = FITTERS_BY_ID[fitter_id]
        if state.fit_thread is not None:
            # warn that fit is alread running...
            print("fit already running...")
            await log("Can't start fit, a fit is already running...")
            return
        
        # TODO: better access to model parameters
        num_params = len(fitProblem.getp())
        if num_params == 0:
            raise ValueError("Problem has no fittable parameters")

        # Start a new thread worker and give fit problem to the worker.
        # Clear abort and uncertainty state
        # state.abort = False
        # state.fitting.uncertainty_state = None
        num_steps = get_num_steps(fitter_id, num_params, options)
        state.abort_queue = abort_queue = Queue()
        fit_thread = FitThread(
            abort_queue=abort_queue,
            problem=fitProblem,
            fitclass=fitclass,
            options=options,
            # session_id=session_id,
            # Number of seconds between updates to the GUI, or 0 for no updates
            convergence_update=5,
            uncertainty_update=3600,
            )
        fit_thread.start()
        state.fit_thread = fit_thread
        await sio.emit("fit_progress", {}) # clear progress
        await publish("", "fit_active", to_json_compatible_dict(dict(fitter_id=fitter_id, options=options, num_steps=num_steps)))
        await log(json.dumps(to_json_compatible_dict(options), indent=2), title = f"starting fitter {fitter_id}")

async def _fit_progress_handler(event: Dict):
    # session_id = event["session_id"]
    problem_state = state.problem
    fitProblem = problem_state.fitProblem if problem_state is not None else None
    if fitProblem is None:
        raise ValueError("should never happen: fit progress reported for session in which fitProblem is undefined")
    message = event.get("message", None)
    if message == 'complete' or message == 'improvement':
        fitProblem.setp(event["point"])
        fitProblem.model_update()
        await publish("", "update_parameters", True)
        if message == 'complete':
            await publish("", "fit_active", {})
    elif message == 'convergence_update':
        state.fitting.population = event["pop"]
        await publish("", "convergence_update", True)
    elif message == 'progress':
        await sio.emit("fit_progress", to_json_compatible_dict(event))
    elif message == 'uncertainty_update' or message == 'uncertainty_final':
        state.fitting.uncertainty_state = cast(bumps.dream.state.MCMCDraw, event["uncertainty_state"])
        await publish("", "uncertainty_update", True)

def fit_progress_handler(event: Dict):
    asyncio.run_coroutine_threadsafe(_fit_progress_handler(event), app.loop)

EVT_FIT_PROGRESS.connect(fit_progress_handler)

async def _fit_complete_handler(event):
    print("complete event: ", event.get("message", ""))
    message = event.get("message", None)
    fit_thread = state.fit_thread
    if fit_thread is not None:
        # print(fit_thread)
        fit_thread.join(1) # 1 second timeout on join
        if fit_thread.is_alive():
            await log("fit thread failed to complete")
    state.fit_thread = None
    problem: refl1d.fitproblem.FitProblem = event["problem"]
    chisq = nice(2*event["value"]/problem.dof)
    problem.setp(event["point"])
    problem.model_update()
    await publish("", "fit_active", {})
    await publish("", "update_parameters", True)
    await log(event["info"], title=f"done with chisq {chisq}")

def fit_complete_handler(event: Dict):
    asyncio.run_coroutine_threadsafe(_fit_complete_handler(event), app.loop)

EVT_FIT_COMPLETE.connect(fit_complete_handler)

async def log(message: str, title: Optional[str] = None):
    await publish("", "log", {"message": message, "title": title})

def get_single_probe_data(theory, probe, substrate=None, surface=None, label=''):
    fresnel_calculator = probe.fresnel(substrate, surface)
    Q, FQ = probe.apply_beam(probe.calc_Q, fresnel_calculator(probe.calc_Q))
    Q, R = theory
    output: Dict[str, Union[str, np.ndarray]]
    assert isinstance(FQ, np.ndarray)
    if len(Q) != len(probe.Q):
        # Saving interpolated data
        output = dict(Q = Q, theory = R, fresnel=np.interp(Q, probe.Q, FQ))
    elif getattr(probe, 'R', None) is not None:
        output = dict(Q = probe.Q, dQ = probe.dQ, R = probe.R, dR = probe.dR, theory = R, fresnel = FQ, background=probe.background.value, intensity=probe.intensity.value)
    else:
        output = dict(Q = probe.Q, dQ = probe.dQ, theory = R, fresnel = FQ)
    output['label'] = f"{probe.label()} {label}"
    return output

def get_probe_data(theory, probe, substrate=None, surface=None):
    if isinstance(probe, refl1d.probe.PolarizedNeutronProbe):
        output = []
        for xsi, xsi_th, suffix in zip(probe.xs, theory, ('--', '-+', '+-', '++')):
            if xsi is not None:
                output.append(get_single_probe_data(xsi_th, xsi, substrate, surface, suffix))
        return output
    else:
        return [get_single_probe_data(theory, probe, substrate, surface)]

@sio.event
@rest_get
async def get_plot_data(sid: str="", view: str = 'linear'):
    # TODO: implement view-dependent return instead of doing this in JS
    # (calculate x,y,dy.dx for given view, excluding log)
    if state.problem is None or state.problem.fitProblem is None:
        return None
    fitProblem = state.problem.fitProblem
    chisq = get_chisq(fitProblem)
    plotdata = []
    result = {"chisq": chisq, "plotdata": plotdata}
    for model in fitProblem.models:
        assert(isinstance(model, ExperimentBase))
        theory = model.reflectivity()
        probe = model.probe
        plotdata.append(get_probe_data(theory, probe, model._substrate, model._surface))
        # fresnel_calculator = probe.fresnel(model._substrate, model._surface)
        # Q, FQ = probe.apply_beam(probe.calc_Q, fresnel_calculator(probe.calc_Q))
        # Q, R = theory
        # assert isinstance(FQ, np.ndarray)
        # if len(Q) != len(probe.Q):
        #     # Saving interpolated data
        #     output = dict(Q = Q, R = R, fresnel=np.interp(Q, probe.Q, FQ))
        # elif getattr(probe, 'R', None) is not None:
        #     output = dict(Q = probe.Q, dQ = probe.dQ, R = probe.R, dR = probe.dR, fresnel = FQ)
        # else:
        #     output = dict(Q = probe.Q, dQ = probe.dQ, R = R, fresnel = FQ)
        # result.append(output)
    return to_json_compatible_dict(result)

@sio.event
@rest_get
async def get_model(sid: str=""):
    if state.problem is None or state.problem.fitProblem is None:
        return None
    fitProblem = state.problem.fitProblem
    return serialize(fitProblem)

@sio.event
@rest_get
async def get_profile_plot(sid: str="", model_index: int=0, sample_index: int=0):
    if state.problem is None or state.problem.fitProblem is None:
        return None
    fitProblem = state.problem.fitProblem
    models = list(fitProblem.models)
    if model_index > len(models):
        return None
    model = models[model_index]
    assert (isinstance(model, Union[Experiment, MixedExperiment]))
    if isinstance(model, MixedExperiment):
        model = model.parts[sample_index]
    fig = plot_sld_profile_plotly(model)
    return to_json_compatible_dict(fig.to_dict())

@sio.event
@rest_get
async def get_profile_data(sid: str="", model_index: int=0):
    if state.problem is None or state.problem.fitProblem is None:
        return None
    fitProblem = state.problem.fitProblem
    models = list(fitProblem.models)
    if (model_index > len(models)):
        return None
    model = models[model_index]
    assert(isinstance(model, ExperimentBase))
    output = {}
    output["ismagnetic"] = model.ismagnetic
    if model.ismagnetic:
        if not model.step_interfaces:
            z, rho, irho, rhoM, thetaM = model.magnetic_step_profile()
            output['step_profile'] = dict(z=z, rho=rho, irho=irho, rhoM=rhoM, thetaM=thetaM)
        z, rho, irho, rhoM, thetaM = model.magnetic_smooth_profile()
        output['smooth_profile'] = dict(z=z, rho=rho, irho=irho, rhoM=rhoM, thetaM=thetaM)
    else:
        if not model.step_interfaces:
            z, rho, irho = model.step_profile()
            output['step_profile'] = dict(z=z, rho=rho, irho=irho)
        z, rho, irho = model.smooth_profile()
        output['smooth_profile'] = dict(z=z, rho=rho, irho=irho)
    return to_json_compatible_dict(output)

@sio.event
@rest_get
async def get_convergence_plot(sid: str=""):
    # NOTE: this is slow.  Creating the figure takes around 0.15 seconds, 
    # and converting to mpld3 can take as much as 0.5 seconds.
    # Might want to replace with plotting on the client side (normalizing population takes around 1 ms)
    if state.problem is None or state.problem.fitProblem is None:
        return None
    fitProblem = state.problem.fitProblem
    population = state.fitting.population
    if population is not None:
        import mpld3
        import matplotlib
        matplotlib.use("agg")
        import matplotlib.pyplot as plt
        import time
        start_time = time.time()
        print('queueing new convergence plot...', start_time)

        normalized_pop = 2*population/fitProblem.dof
        best, pop = normalized_pop[:, 0], normalized_pop[:, 1:]
        print("time to normalize population: ", time.time() - start_time)
        fig = plt.figure()
        axes = fig.add_subplot(111)

        ni,npop = pop.shape
        iternum = np.arange(1,ni+1)
        tail = int(0.25*ni)
        c = bumps.plotutil.coordinated_colors(base=(0.4,0.8,0.2))
        if npop==5:
            axes.fill_between(iternum[tail:], pop[tail:,1], pop[tail:,3],
                                color=c['light'], label='_nolegend_')
            axes.plot(iternum[tail:],pop[tail:,2],
                        label="80% range", color=c['base'])
            axes.plot(iternum[tail:],pop[tail:,0],
                        label="_nolegend_", color=c['base'])
        axes.plot(iternum[tail:], best[tail:], label="best",
                    color=c['dark'])
        axes.set_xlabel('iteration number')
        axes.set_ylabel('chisq')
        axes.legend()
        #plt.gca().set_yscale('log')
        # fig.draw()
        print("time to render but not serialize...", time.time() - start_time)
        dfig = mpld3.fig_to_dict(fig)
        plt.close(fig)
        # await sio.emit("profile_plot", dfig, to=sid)
        end_time = time.time()
        print("time to draw convergence plot:", end_time - start_time)
        return dfig
    else:
        return None

@sio.event
@rest_get
async def get_correlation_plot(sid: str = "", nbins: int=50):
    from .corrplot import Corr2d
    uncertainty_state = state.fitting.uncertainty_state

    if isinstance(uncertainty_state, bumps.dream.state.MCMCDraw):
        import time
        start_time = time.time()
        print('queueing new correlation plot...', start_time)
        draw = uncertainty_state.draw()
        c = Corr2d(draw.points.T, bins=nbins, labels=draw.labels)
        fig = c.plot()
        print("time to render but not serialize...", time.time() - start_time)
        serialized = to_json_compatible_dict(fig.to_dict())
        end_time = time.time()
        print("time to draw correlation plot:", end_time - start_time)
        return serialized
    else:
        return None

@sio.event
@rest_get
async def get_uncertainty_plot(sid: str = ""):
    uncertainty_state = state.fitting.uncertainty_state
    if uncertainty_state is not None:
        import time
        start_time = time.time()
        print('queueing new uncertainty plot...', start_time)
        draw = uncertainty_state.draw()
        stats = bumps.dream.stats.var_stats(draw)
        fig = plot_vars(draw, stats)
        return to_json_compatible_dict(fig.to_dict())
    else:
        return None

@sio.event
@rest_get
async def get_model_uncertainty_plot(sid: str = ""):
    if state.problem is None or state.problem.fitProblem is None:
        return None
    fitProblem = state.problem.fitProblem
    uncertainty_state = state.fitting.uncertainty_state
    if uncertainty_state is not None:
        import mpld3
        import matplotlib
        matplotlib.use("agg")
        import matplotlib.pyplot as plt
        import time
        start_time = time.time()
        print('queueing new model uncertainty plot...', start_time)

        fig = plt.figure()
        errs = bumps.errplot.calc_errors_from_state(fitProblem, uncertainty_state)
        print('errors calculated: ', time.time() - start_time)
        bumps.errplot.show_errors(errs, fig=fig)
        print("time to render but not serialize...", time.time() - start_time)
        fig.canvas.draw()
        dfig = mpld3.fig_to_dict(fig)
        plt.close(fig)
        # await sio.emit("profile_plot", dfig, to=sid)
        end_time = time.time()
        print("time to draw model uncertainty plot:", end_time - start_time)
        return dfig
    else:
        return None

@sio.event
@rest_get
async def get_parameter_trace_plot(sid: str=""):
    uncertainty_state = state.fitting.uncertainty_state
    if uncertainty_state is not None:
        import mpld3
        import matplotlib
        matplotlib.use("agg")
        import matplotlib.pyplot as plt
        import time

        start_time = time.time()
        print('queueing new parameter_trace plot...', start_time)

        fig = plt.figure()
        axes = fig.add_subplot(111)

        # begin plotting:
        var = 0
        portion = None
        draw, points, _ = uncertainty_state.chains()
        label = uncertainty_state.labels[var]
        start = int((1-portion)*len(draw)) if portion else 0
        genid = np.arange(uncertainty_state.generation-len(draw)+start, uncertainty_state.generation)+1
        axes.plot(genid*uncertainty_state.thinning,
             np.squeeze(points[start:, uncertainty_state._good_chains, var]))
        axes.set_xlabel('Generation number')
        axes.set_ylabel(label)
        fig.canvas.draw()

        print("time to render but not serialize...", time.time() - start_time)
        dfig = mpld3.fig_to_dict(fig)
        plt.close(fig)
        # await sio.emit("profile_plot", dfig, to=sid)
        end_time = time.time()
        print("time to draw parameter_trace plot:", end_time - start_time)
        return dfig
    else:
        return None
    

@sio.event
@rest_get
async def get_parameters(sid: str = "", only_fittable: bool = False):
    if state.problem is None or state.problem.fitProblem is None:
        return []
    fitProblem = state.problem.fitProblem

    all_parameters = fitProblem.model_parameters()
    if only_fittable:
        parameter_infos = params_to_list(unique(all_parameters))
        # only include params with priors:
        parameter_infos = [pi for pi in parameter_infos if pi['fittable'] and not pi['fixed']]
    else:
        parameter_infos = params_to_list(all_parameters)
        
    return to_json_compatible_dict(parameter_infos)

@sio.event
async def set_parameter(sid: str, parameter_id: str, property: Literal["value01", "value", "min", "max"], value: Union[float, str, bool]):
    if state.problem is None or state.problem.fitProblem is None:
        return None
    fitProblem = state.problem.fitProblem

    parameter = fitProblem._parameters_by_id.get(parameter_id, None)
    if parameter is None:
        warnings.warn(f"Attempting to update parameter that doesn't exist: {parameter_id}")
        return

    if parameter.prior is None:
        warnings.warn(f"Attempting to set prior properties on parameter without priors: {parameter}")
        return

    if property == "value01":
        new_value  = parameter.prior.put01(value)
        nice_new_value = nice(new_value, digits=VALUE_PRECISION)
        parameter.clip_set(nice_new_value)
    elif property == "value":
        new_value = float(value)
        nice_new_value = nice(new_value, digits=VALUE_PRECISION)
        parameter.clip_set(nice_new_value)
    elif property == "min":
        lo = float(value)
        hi = parameter.prior.limits[1]
        parameter.range(lo, hi)
        parameter.add_prior()
    elif property == "max":
        lo = parameter.prior.limits[0]
        hi = float(value)
        parameter.range(lo, hi)
        parameter.add_prior()
    elif property == "fixed":
        if parameter.fittable:
            parameter.fixed = bool(value)
            fitProblem.model_reset()
            # print(f"setting parameter: {parameter}.fixed to {value}")
            # model has been changed: setp and getp will return different values!
            await publish("", "update_model", True)
    fitProblem.model_update()
    await publish("", "update_parameters", True)
    return

@sio.event
async def publish(sid: str, topic: TopicNameType, message=None):
    timestamp_str = f"{datetime.now().timestamp():.6f}"
    contents = {"message": message, "timestamp": timestamp_str}
    # session = get_session(session_id)    
    state.topics[topic].append(contents)
    # if session_id == app["active_session"]:
    #     await sio.emit(topic, contents)
    await sio.emit(topic, contents)
    # print("emitted: ", topic, contents)


@sio.event
@rest_get
async def get_topic_messages(sid: str="", topic: Optional[TopicNameType] = None, max_num=None) -> List[Dict]:
    # this is a GET request in disguise -
    # emitter must handle the response in a callback,
    # as no separate response event is emitted.
    if topic is None:
        return []    
    topics = state.topics
    q = topics.get(topic, None)
    if q is None:
        raise ValueError(f"Topic: {topic} not defined")
    elif max_num is None:
        return list(q)
    else:
        q_length = len(q)
        start = max(q_length - max_num, 0)
        return list(itertools.islice(q, start, q_length))

@sio.event
@rest_get
async def get_dirlisting(sid: str="", pathlist: Optional[List[str]]=None):
    # GET request
    # TODO: use psutil to get disk listing as well?
    if pathlist is None:
        pathlist = []
    subfolders = []
    files = []
    for p in Path(*pathlist).iterdir():
        if p.is_dir():
            subfolders.append(p.name)
        else:
            # files.append(p.resolve().name)
            files.append(p.name)
    return dict(subfolders=subfolders, files=files)

@sio.event
@rest_get
async def get_current_pathlist(sid: str="") -> Optional[List[str]]:
    problem_state = state.problem
    pathlist = problem_state.pathlist if problem_state is not None else None
    return pathlist

@sio.event
@rest_get
async def get_fitter_defaults(sid: str=""):
    return FITTER_DEFAULTS

@sio.event
def disconnect(sid):
    print('disconnect ', sid)

async def disconnect_all_clients():
    # disconnect all clients:
    clients = list(sio.manager.rooms.get('/', {None: {}}).get(None).keys())
    for client in clients:
        await sio.disconnect(client)

@sio.event
async def shutdown(sid: str=""):
    print("killing...")
    signal.raise_signal(signal.SIGTERM)

if Path.exists(static_assets_path):
    app.router.add_static('/assets', static_assets_path)
app.router.add_get('/', index)

VALUE_PRECISION = 6
VALUE_FORMAT = "{{:.{:d}g}}".format(VALUE_PRECISION)

def nice(v, digits=4):
    """Fix v to a value with a given number of digits of precision"""
    from math import log10, floor
    if v == 0. or not np.isfinite(v):
        return v
    else:
        sign = v/abs(v)
        place = floor(log10(abs(v)))
        scale = 10**(place-(digits-1))
        return sign*floor(abs(v)/scale+0.5)*scale


def to_json_compatible_dict(obj):
    if isinstance(obj, (list, tuple)):
        return type(obj)(to_json_compatible_dict(v) for v in obj)
    elif isinstance(obj, GeneratorType):
        return list(to_json_compatible_dict(v) for v in obj)
    elif isinstance(obj, dict):
        return type(obj)((to_json_compatible_dict(k), to_json_compatible_dict(v))
                        for k, v in obj.items())
    elif isinstance(obj, np.ndarray) and obj.dtype.kind in ['f', 'i']:
        return obj.tolist()
    elif isinstance(obj, np.ndarray) and obj.dtype.kind == 'O':
        return to_json_compatible_dict(obj.tolist())
    elif isinstance(obj, float):
        return str(obj) if np.isinf(obj) else obj
    elif isinstance(obj, int) or isinstance(obj, str) or obj is None:
        return obj
    else:
        raise ValueError("obj %s is not serializable" % str(obj))


class ParamInfo(TypedDict, total=False):
    id: str
    name: str
    paths: List[str]
    value_str: str
    fittable: bool
    fixed: bool
    writable: bool
    value01: float
    min_str: str
    max_str: str


def params_to_list(params, lookup=None, pathlist=None, links=None) -> List[ParamInfo]:
    lookup: Dict[str, ParamInfo] = {} if lookup is None else lookup
    pathlist = [] if pathlist is None else pathlist
    if isinstance(params,dict):
        for k in sorted(params.keys()):
            params_to_list(params[k], lookup=lookup, pathlist=pathlist + [k])
    elif isinstance(params, tuple) or isinstance(params, list):
        for i, v in enumerate(params):
            params_to_list(v, lookup=lookup, pathlist=pathlist + [f"[{i:d}]"])
    elif isinstance(params, Parameter):
        path = ".".join(pathlist)
        existing = lookup.get(params.id, None)
        if existing is not None:
            existing["paths"].append(".".join(pathlist))
        else:
            value_str = VALUE_FORMAT.format(nice(params.value))
            has_prior = params.has_prior()
            new_item: ParamInfo = { 
                "id": params.id,
                "name": str(params.name),
                "paths": [path],
                "writable": type(params.slot) in [Variable, Parameter], 
                "value_str": value_str, "fittable": params.fittable, "fixed": params.fixed }
            if has_prior:
                assert(params.prior is not None)
                lo, hi = params.prior.limits
                new_item['value01'] = params.prior.get01(params.value)
                new_item['min_str'] = VALUE_FORMAT.format(nice(lo))
                new_item['max_str'] = VALUE_FORMAT.format(nice(hi))
            lookup[params.id] = new_item
    return list(lookup.values())

import argparse

def main():
    parser = argparse.ArgumentParser()
    # parser.add_argument('-d', '--debug', action='store_true', help='autoload modules on change')
    parser.add_argument('-x', '--headless', action='store_true', help='do not automatically load client in browser')
    parser.add_argument('--external', action='store_true', help='listen on all interfaces, including external (local connections only if not set)')
    parser.add_argument('-p', '--port', default=0, type=int, help='port on which to start the server')
    parser.add_argument('--hub', default=None, type=str, help='api address of parent hub (only used when called as subprocess)')
    # parser.add_argument('-c', '--config-file', type=str, help='path to JSON configuration to load')
    args = parser.parse_args()

    # app.on_startup.append(lambda App: publish('', 'local_file_path', Path().absolute().parts))
    async def notice(message: str):
        print(message)
    app.on_cleanup.append(lambda App: notice("cleanup task"))
    app.on_shutdown.append(lambda App: notice("shutdown task"))
    app.on_shutdown.append(lambda App: stop_fit())
    app.on_shutdown.append(lambda App: disconnect_all_clients())
    app.on_shutdown.append(lambda App: state.cleanup())
    # set initial path to cwd:
    state.problem.pathlist = list(Path().absolute().parts)
    app.add_routes(routes)
    hostname = 'localhost' if not args.external else '0.0.0.0'

    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((hostname, args.port))
    host, port = sock.getsockname()
    state.hostname = host
    state.port = port
    if args.hub is not None:
        async def register_instance(application: web.Application):
            async with ClientSession() as client_session:
                await client_session.post(args.hub, json={"host": hostname, "port": port})
        app.on_startup.append(register_instance)
    if not args.headless:
        import webbrowser
        async def open_browser(app: web.Application):
            app.loop.call_later(0.25, lambda: webbrowser.open_new_tab(f"http://{hostname}:{port}/"))
        app.on_startup.append(open_browser)
    web.run_app(app, sock=sock)

if __name__ == '__main__':
    main()
