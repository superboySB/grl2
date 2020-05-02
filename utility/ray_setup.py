import signal
import sys
import psutil
import ray


def sigint_shutdown_ray():
    """ Shutdown ray when the process is terminated by ctrl+C """
    def handler(sig, frame):
        if ray.is_initialized():
            ray.shutdown()
            print('ray has been shutdown by sigint')
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)

def cpu_affinity(name=None):
    resources = ray.get_resource_ids()
    cpus = [v[0] for v in resources['CPU']]
    psutil.Process().cpu_affinity(cpus)
    if name:
        print(f'CPUs corresponding to {name}: {cpus}')