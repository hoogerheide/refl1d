# from .main import setup_bumps

from typing import Dict, List, Literal, Optional, Union, TypedDict
from datetime import datetime
import warnings
from queue import Queue
from aiohttp import web
import numpy as np
import asyncio
import socketio
from pathlib import Path, PurePath
import json
from copy import deepcopy
from blinker import Signal

import mimetypes
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/html", ".html")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/svg+xml", ".svg")

from bumps.fitters import DreamFit, LevenbergMarquardtFit, SimplexFit, DEFit, MPFit, BFGSFit, FitDriver, fit
from bumps.serialize import to_dict
from bumps.mapper import MPMapper
from bumps.parameter import Parameter, Variable, unique
import bumps.fitproblem
import refl1d.fitproblem, refl1d.probe
from refl1d.experiment import Experiment

from .fit_thread import FitThread, EVT_FIT_COMPLETE, EVT_FIT_PROGRESS

# can get by name and not just by id
EVT_LOG = Signal('log')

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
static_dir_path = Path(__file__).parent.parent / 'client' / 'dist'
sio.attach(app)

topics: Dict[str, Dict] = {}
app["topics"] = topics
app["problem"] = {"fitProblem": None, "filepath": None}
app["fitting"] = {
    "fit_thread": None,
    "abort": False,
    "uncertainty_state": None,
    "abort_queue": Queue(),
}
problem = None


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
    # redirect to static built site:
    return web.HTTPFound('/static/index.html')
    
@sio.event
async def connect(sid, environ, data=None):
    # re-send last message for all topics
    for topic, contents in topics.items():
        await sio.emit(topic, contents, to=sid)
    print("connect ", sid)

@sio.event
async def load_problem_file(sid: str, pathlist: List[str], filename: str):
    from bumps.cli import load_model
    path = Path(*pathlist, filename)
    print('model loading: ', str(path))
    problem = load_model(str(path))
    app["problem"]["fitProblem"] = problem
    print('model loaded: ', str(path))

    model_names = [getattr(m, 'name', None) for m in list(problem.models)]
    await publish("", "model_loaded", {"pathlist": pathlist, "filename": filename, "model_names": model_names})
    await publish("", "update_model", True)
    await publish("", "update_parameters", True)

@sio.event
async def start_fit(sid: str="", fitter_id: str="", kwargs=None):
    kwargs = {} if kwargs is None else kwargs
    fitProblem: refl1d.fitproblem.FitProblem = app["problem"]["fitProblem"]
    mapper = MPMapper.start_mapper(fitProblem, None, cpus=0)
    monitors = []
    fitclass = FITTERS_BY_ID[fitter_id]
    driver = FitDriver(fitclass=fitclass, mapper=mapper, problem=fitProblem, monitors=monitors, **kwargs)
    x, fx = driver.fit()
    driver.show()

@sio.event
async def stop_fit(sid: str):
    abort_queue: Queue = app["fitting"]["abort_queue"]
    abort_queue.put(True)

@sio.event
async def start_fit_thread(sid: str="", fitter_id: str="", options=None):
    options = {} if options is None else options
    fitProblem: refl1d.fitproblem.FitProblem = app["problem"]["fitProblem"]
    fitclass = FITTERS_BY_ID[fitter_id]
    if app["problem"].get("fit_thread", None) is not None:
        # warn that fit is alread running...
        print("fit already running...")
        return
    
    # TODO: better access to model parameters
    if len(fitProblem.getp()) == 0:
        raise ValueError("Problem has no fittable parameters")

    # Start a new thread worker and give fit problem to the worker.
    # Clear abort and uncertainty state
    app["fitting"]["abort"] = False
    app["fitting"]["uncertainty_state"] = None
    abort_queue: Queue = app["fitting"]["abort_queue"]
    for _ in range(abort_queue.qsize()):
        abort_queue.get_nowait()
        abort_queue.task_done()
    fit_thread = FitThread(
        abort_queue=abort_queue,
        problem=fitProblem,
        fitclass=fitclass,
        options=options,
        # Number of seconds between updates to the GUI, or 0 for no updates
        convergence_update=5,
        uncertainty_update=3600,
        )
    fit_thread.start()
    await publish("", "fit_active", True)

def fit_progress_handler(event):
    print("event: ", event)
    message = event.get("message", None)
    if message == 'complete' or message == 'improvement':
        fitProblem: refl1d.fitproblem.FitProblem = app["problem"]["fitProblem"]
        fitProblem.setp(event["point"])
        fitProblem.model_update()
        asyncio.run_coroutine_threadsafe(publish("", "update_parameters", True), app.loop)
    if message == 'complete':
        asyncio.run_coroutine_threadsafe(publish("", "fit_active", False), app.loop)

EVT_FIT_PROGRESS.connect(fit_progress_handler)

def fit_complete_handler(event):
    print("event: ", event)
    message = event.get("message", None)
    fit_thread = app["fitting"]["fit_thread"]
    if fit_thread is not None:
        fit_thread.join(1) # 1 second timeout on join
        if fit_thread.is_alive():
            EVT_LOG.send("fit thread failed to complete")
    app["fitting"]["fit_thread"] = None
    problem: refl1d.fitproblem.FitProblem = event["problem"]
    chisq = nice(2*event["value"]/problem.dof)
    problem.setp(event["point"])
    problem.model_update()
    asyncio.run_coroutine_threadsafe(publish("", "update_parameters", True), app.loop)
    EVT_LOG.send("done with chisq %g"%chisq)
    EVT_LOG.send(event["info"])

EVT_FIT_COMPLETE.connect(fit_complete_handler)

def log_handler(message):
    asyncio.run_coroutine_threadsafe(publish("", "log", message), app.loop)

EVT_LOG.connect(log_handler)

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
    output['label'] = label
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
    fitProblem: refl1d.fitproblem.FitProblem = app["problem"]["fitProblem"]
    result = []
    for model in fitProblem.models:
        assert(isinstance(model, Experiment))
        theory = model.reflectivity()
        probe = model.probe
        result.append(get_probe_data(theory, probe, model._substrate, model._surface))
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
    return to_dict(result)

@sio.event
@rest_get
async def get_model(sid: str=""):
    from bumps.serialize import to_dict
    fitProblem: refl1d.fitproblem.FitProblem = app["problem"]["fitProblem"]
    return to_dict(fitProblem)

@sio.event
@rest_get
async def get_profile_plot(sid: str="", model_index: int=0):
    import mpld3
    import matplotlib
    matplotlib.use("agg")
    import matplotlib.pyplot as plt
    import time
    print('queueing new profile plot...', time.time())
    fitProblem: refl1d.fitproblem.FitProblem = app["problem"]["fitProblem"]
    if fitProblem is None:
        return None
    models = list(fitProblem.models)
    if (model_index > len(models)):
        return None
    model = models[model_index]
    assert(isinstance(model, Experiment))
    fig = plt.figure()
    model.plot_profile()
    dfig = mpld3.fig_to_dict(fig)
    plt.close(fig)
    # await sio.emit("profile_plot", dfig, to=sid)
    return dfig

@sio.event
@rest_get
async def get_profile_data(sid: str="", model_index: int=0):
    fitProblem: refl1d.fitproblem.FitProblem = app["problem"]["fitProblem"]
    if fitProblem is None:
        return None
    models = list(fitProblem.models)
    if (model_index > len(models)):
        return None
    model = models[model_index]
    assert(isinstance(model, Experiment))
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
    return to_dict(output)

@sio.event
@rest_get
async def get_parameters(sid: str = "", only_fittable: bool = False):
    fitProblem: refl1d.fitproblem.FitProblem = app["problem"]["fitProblem"]
    if fitProblem is None:
        return []

    all_parameters = fitProblem.model_parameters()
    if only_fittable:
        parameter_infos = params_to_list(unique(all_parameters))
        # only include params with priors:
        parameter_infos = [pi for pi in parameter_infos if pi['fittable'] and not pi['fixed']]
    else:
        parameter_infos = params_to_list(all_parameters)
        
    return to_dict(parameter_infos)

@sio.event
async def set_parameter(sid: str, parameter_id: str, property: Literal["value01", "value", "min", "max"], value: Union[float, str, bool]):
    fitProblem: bumps.fitproblem.FitProblem = app["problem"]["fitProblem"]
    if fitProblem is None:
        return

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
            print(f"setting parameter: {parameter}.fixed to {value}")
    fitProblem.model_update()
    await publish("", "update_parameters", True)
    return

@sio.event
async def publish(sid: str, topic: str, message):
    timestamp_str = f"{datetime.now().timestamp():.6f}"
    contents = {"message": message, "timestamp": timestamp_str}
    topics[topic] = contents
    await sio.emit(topic, contents)
    # print("emitted: ", topic, contents)

@sio.event
@rest_get
async def get_last_message(sid: str="", topic: str=""):
    # this is a GET request in disguise -
    # emitter must handle the response in a callback,
    # as no separate response event is emitted.  
    return topics.get(topic, {})

@rest_get
async def get_all_messages():
    return topics

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
async def get_fitter_defaults(sid: str=""):
    return FITTER_DEFAULTS

@sio.event
def disconnect(sid):
    print('disconnect ', sid)

app.router.add_static('/static', static_dir_path)
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
    

class ParamInfo(TypedDict):
    id: str
    name: str
    paths: List[str]
    value_str: str
    fittable: bool
    fixed: bool
    writable: bool
    value01: Optional[float]
    min_str: Optional[str]
    max_str: Optional[str]


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

def main():
    app.on_startup.append(lambda App: publish('', 'local_file_path', Path().absolute().parts))
    app.add_routes(routes)
    web.run_app(app)

if __name__ == '__main__':
    main()