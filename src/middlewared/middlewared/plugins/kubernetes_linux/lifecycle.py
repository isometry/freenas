import asyncio
import errno
import json
import os
import shutil
import uuid

from datetime import datetime

from middlewared.service import CallError, private, Service

START_LOCK = asyncio.Lock()


class KubernetesService(Service):

    @private
    async def post_start(self):
        # TODO: Add support for migrations
        async with START_LOCK:
            return await self.post_start_impl()

    @private
    async def post_start_impl(self):
        try:
            timeout = 60
            while timeout > 0:
                node_config = await self.middleware.call('k8s.node.config')
                if node_config['node_configured']:
                    break
                else:
                    await asyncio.sleep(2)
                    timeout -= 2

            if not node_config['node_configured']:
                raise CallError(f'Unable to configure node: {node_config["error"]}')
            await self.post_start_internal()
        except Exception as e:
            await self.middleware.call('alert.oneshot_create', 'ApplicationsStartFailed', {'error': str(e)})
            raise
        else:
            asyncio.ensure_future(self.middleware.call('k8s.event.setup_k8s_events'))
            await self.middleware.call('chart.release.refresh_events_state')
            await self.middleware.call('alert.oneshot_delete', 'ApplicationsStartFailed', None)

    @private
    async def post_start_internal(self):
        await self.middleware.call('k8s.node.add_taints', [{'key': 'ix-svc-start', 'effect': 'NoExecute'}])
        node_config = await self.middleware.call('k8s.node.config')
        await self.middleware.call('k8s.cni.setup_cni')
        await self.middleware.call('k8s.gpu.setup')
        await self.middleware.call('k8s.storage_class.setup_default_storage_class')
        await self.middleware.call(
            'k8s.node.remove_taints', [
                k['key'] for k in (node_config['spec']['taints'] or []) if k['key'] in ('ix-svc-start', 'ix-svc-stop')
            ]
        )
        # FIXME: Let's please remove the sleep and check on pods to see that they have started executing once
        # we add support to retrieve workloads
        # We should wait for around 20 seconds so that pods are scheduled and start executing on the node as it is
        # then that kube-router configures routes in the main table which we would like to add to kube-router table
        # because it's internal traffic will also be otherwise advertised to the default route specified
        await asyncio.sleep(20)
        await self.middleware.call('k8s.cni.add_routes_to_kube_router_table')

    @private
    async def validate_k8s_fs_setup(self):
        config = await self.middleware.call('kubernetes.config')
        if not await self.middleware.call('pool.query', [['name', '=', config['pool']]]):
            raise CallError(f'"{config["pool"]}" pool not found.', errno=errno.ENOENT)

        k8s_datasets = set(await self.kubernetes_datasets(config['dataset']))
        required_datasets = set(config['dataset']) | set(
            os.path.join(config['dataset'], ds) for ds in ('k3s', 'docker', 'releases')
        )
        diff = {
            d['id'] for d in await self.middleware.call('zfs.dataset.query', [['id', 'in', list(k8s_datasets)]])
        } ^ k8s_datasets
        fatal_diff = diff.intersection(required_datasets)
        if fatal_diff:
            raise CallError(f'Missing "{", ".join(fatal_diff)}" dataset(s) required for starting kubernetes.')

        if diff:
            await self.create_update_k8s_datasets(config['dataset'])

        locked_datasets = [
            d['id'] for d in filter(
                lambda d: d['mountpoint'], await self.middleware.call('zfs.dataset.locked_datasets')
            )
            if d['mountpoint'].startswith(f'{config["dataset"]}/') or d['mountpoint'] in (
                f'/mnt/{k}' for k in (config['dataset'], config['pool'])
            )
        ]
        if locked_datasets:
            raise CallError(
                f'Please unlock following dataset(s) before starting kubernetes: {", ".join(locked_datasets)}'
            )
        iface_errors = await self.middleware.call('kubernetes.validate_interfaces', config)
        if iface_errors:
            raise CallError(f'Unable to lookup configured interfaces: {", ".join([v[1] for v in iface_errors])}')

    @private
    def status_change(self):
        config = self.middleware.call_sync('kubernetes.config')
        if self.middleware.call_sync('service.started', 'kubernetes'):
            self.middleware.call_sync('service.stop', 'kubernetes')

        if not config['pool']:
            return

        config_path = os.path.join('/mnt', config['dataset'], 'config.json')
        clean_start = True
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                on_disk_config = json.loads(f.read())
            clean_start = not all(
                config[k] == on_disk_config.get(k) for k in ('cluster_cidr', 'service_cidr', 'cluster_dns_ip')
            )

        if clean_start and self.middleware.call_sync('zfs.dataset.query', [['id', '=', config['dataset']]]):
            self.middleware.call_sync('zfs.dataset.delete', config['dataset'], {'force': True, 'recursive': True})

        self.middleware.call_sync('kubernetes.setup_pool')
        try:
            self.middleware.call_sync('kubernetes.status_change_internal')
        except Exception as e:
            self.middleware.call_sync('alert.oneshot_create', 'ApplicationsConfigurationFailed', {'error': str(e)})
            raise
        else:
            with open(config_path, 'w') as f:
                f.write(json.dumps(config))

            self.middleware.call_sync('catalog.sync_all')
            self.middleware.call_sync('alert.oneshot_delete', 'ApplicationsConfigurationFailed', None)

    @private
    async def status_change_internal(self):
        await self.validate_k8s_fs_setup()
        await self.middleware.call('service.start', 'docker')
        await self.middleware.call('container.image.load_default_images')
        await self.middleware.call('service.start', 'kubernetes')

    @private
    async def setup_pool(self):
        config = await self.middleware.call('kubernetes.config')
        await self.create_update_k8s_datasets(config['dataset'])
        # Now we would like to setup catalogs
        await self.middleware.call('catalog.sync_all')

    @private
    async def create_update_k8s_datasets(self, k8s_ds):
        for dataset in await self.kubernetes_datasets(k8s_ds):
            if not await self.middleware.call('zfs.dataset.query', [['id', '=', dataset]]):
                test_path = os.path.join('/mnt', dataset)
                if os.path.exists(test_path):
                    await self.middleware.run_in_thread(
                        shutil.move, test_path, f'{test_path}-{str(uuid.uuid4())[:4]}-{datetime.now().isoformat()}',
                    )
                await self.middleware.call('zfs.dataset.create', {'name': dataset, 'type': 'FILESYSTEM'})

    @private
    async def kubernetes_datasets(self, k8s_ds):
        return [k8s_ds] + [
            os.path.join(k8s_ds, d) for d in ('docker', 'k3s', 'releases', 'default_volumes', 'catalogs')
        ]

    @private
    async def start_kubernetes(self):
        # TODO: Remove this wrapper after 21.02 where we will moving on assume that all agent nodes are now using
        #  our constant worker node password
        if not (await self.middleware.call('kubernetes.config'))['pool']:
            return

        await self.middleware.call('service.start', 'kubernetes')
        async with START_LOCK:
            await self.middleware.call('k8s.node.delete_node')
            await self.middleware.call('service.restart', 'kubernetes')
            await self.middleware.call('catalog.sync_all')


async def _event_system(middleware, event_type, args):

    if args['id'] == 'ready':
        asyncio.ensure_future(middleware.call('kubernetes.start_kubernetes'))
    elif args['id'] == 'shutdown' and await middleware.call('service.started', 'kubernetes'):
        asyncio.ensure_future(middleware.call('service.stop', 'kubernetes'))


async def setup(middleware):
    middleware.event_subscribe('system', _event_system)
