# Copyright 2013, 2014 Intel Corporation.
# All Rights Reserved.
#
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import inspect
import six
import yaml

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
from oslo_log import versionutils
from oslo_utils import excutils

from tacker._i18n import _LE
from tacker.api.v1 import attributes
from tacker.common import driver_manager
from tacker.common import exceptions
from tacker.common import utils
from tacker.db.vm import vm_db
from tacker.extensions import vnfm
from tacker.plugins.common import constants
from tacker.vnfm.mgmt_drivers import constants as mgmt_constants
from tacker.vnfm import monitor
from tacker.vnfm import vim_client

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


def config_opts():
    return [('tacker', VNFMMgmtMixin.OPTS),
            ('tacker', VNFMPlugin.OPTS)]


class VNFMMgmtMixin(object):
    OPTS = [
        cfg.ListOpt(
            'mgmt_driver', default=['noop', 'openwrt'],
            help=_('MGMT driver to communicate with '
                   'Hosting VNF/logical service '
                   'instance tacker plugin will use')),
        cfg.IntOpt('boot_wait', default=30,
            help=_('Time interval to wait for VM to boot'))
    ]
    cfg.CONF.register_opts(OPTS, 'tacker')

    def __init__(self):
        super(VNFMMgmtMixin, self).__init__()
        self._mgmt_manager = driver_manager.DriverManager(
            'tacker.tacker.mgmt.drivers', cfg.CONF.tacker.mgmt_driver)

    def _invoke(self, vnf_dict, **kwargs):
        method = inspect.stack()[1][3]
        return self._mgmt_manager.invoke(
            self._mgmt_driver_name(vnf_dict), method, **kwargs)

    def mgmt_create_pre(self, context, vnf_dict):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict)

    def mgmt_create_post(self, context, vnf_dict):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict)

    def mgmt_update_pre(self, context, vnf_dict):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict)

    def mgmt_update_post(self, context, vnf_dict):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict)

    def mgmt_delete_pre(self, context, vnf_dict):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict)

    def mgmt_delete_post(self, context, vnf_dict):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict)

    def mgmt_get_config(self, context, vnf_dict):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict)

    def mgmt_url(self, context, vnf_dict):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict)

    def mgmt_call(self, context, vnf_dict, kwargs):
        return self._invoke(
            vnf_dict, plugin=self, context=context, vnf=vnf_dict,
            kwargs=kwargs)


class VNFMPlugin(vm_db.VNFMPluginDb, VNFMMgmtMixin):
    """VNFMPlugin which supports VNFM framework.

    Plugin which supports Tacker framework
    """
    OPTS = [
        cfg.ListOpt(
            'infra_driver', default=['nova', 'heat', 'noop'],
            help=_('Hosting vnf drivers tacker plugin will use')),
    ]
    cfg.CONF.register_opts(OPTS, 'tacker')
    supported_extension_aliases = ['vnfm']

    def __init__(self):
        super(VNFMPlugin, self).__init__()
        self._pool = eventlet.GreenPool()
        self.boot_wait = cfg.CONF.tacker.boot_wait
        self.vim_client = vim_client.VimClient()
        self._vnf_manager = driver_manager.DriverManager(
            'tacker.tacker.device.drivers',
            cfg.CONF.tacker.infra_driver)
        self._vnf_monitor = monitor.VNFMonitor(self.boot_wait)

    def spawn_n(self, function, *args, **kwargs):
        self._pool.spawn_n(function, *args, **kwargs)

    def create_vnfd(self, context, vnfd):
        vnfd_data = vnfd['vnfd']
        template = vnfd_data['attributes'].get('vnfd')
        if isinstance(template, dict):
            # TODO(sripriya) remove this yaml dump once db supports storing
            # json format of yaml files in a separate column instead of
            # key value string pairs in vnf attributes table
            vnfd_data['attributes']['vnfd'] = yaml.safe_dump(
                template)
        elif isinstance(template, str):
            self._report_deprecated_yaml_str()
        if "tosca_definitions_version" not in template:
            versionutils.report_deprecated_feature(LOG, 'VNFD legacy vnfds'
                ' are deprecated since Mitaka release and will be removed in'
                ' Ocata release. Please use NFV TOSCA vnfds.')

        LOG.debug(_('vnfd %s'), vnfd_data)

        infra_driver = vnfd_data.get('infra_driver')
        if not attributes.is_attr_set(infra_driver):
            LOG.debug(_('hosting vnf driver must be specified'))
            raise vnfm.InfraDriverNotSpecified()
        if infra_driver not in self._vnf_manager:
            LOG.debug(_('unknown hosting vnf driver '
                        '%(infra_driver)s in %(drivers)s'),
                      {'infra_driver': infra_driver,
                       'drivers': cfg.CONF.tacker.infra_driver})
            raise vnfm.InvalidInfraDriver(infra_driver=infra_driver)

        service_types = vnfd_data.get('service_types')
        if not attributes.is_attr_set(service_types):
            LOG.debug(_('service type must be specified'))
            raise vnfm.ServiceTypesNotSpecified()
        for service_type in service_types:
            # TODO(yamahata):
            # framework doesn't know what services are valid for now.
            # so doesn't check it here yet.
            pass

        self._vnf_manager.invoke(
            infra_driver, 'create_vnfd_pre', plugin=self,
            context=context, vnfd=vnfd)

        return super(VNFMPlugin, self).create_vnfd(
            context, vnfd)

    def add_vnf_to_monitor(self, vnf_dict, vim_auth):
        dev_attrs = vnf_dict['attributes']
        mgmt_url = vnf_dict['mgmt_url']
        if 'monitoring_policy' in dev_attrs and mgmt_url:
            def action_cb(hosting_vnf_, action):
                action_cls = monitor.ActionPolicy.get_policy(action,
                                                             vnf_dict)
                if action_cls:
                    action_cls.execute_action(self, hosting_vnf['vnf'],
                                              vim_auth)

            hosting_vnf = self._vnf_monitor.to_hosting_vnf(
                vnf_dict, action_cb)
            LOG.debug('hosting_vnf: %s', hosting_vnf)
            self._vnf_monitor.add_hosting_vnf(hosting_vnf)

    def config_vnf(self, context, vnf_dict):
        config = vnf_dict['attributes'].get('config')
        if not config:
            return
        eventlet.sleep(self.boot_wait)      # wait for vm to be ready
        vnf_id = vnf_dict['id']
        update = {
            'vnf': {
                'id': vnf_id,
                'attributes': {'config': config},
            }
        }
        self.update_vnf(context, vnf_id, update)

    def _create_vnf_wait(self, context, vnf_dict, auth_attr):
        driver_name = self._infra_driver_name(vnf_dict)
        vnf_id = vnf_dict['id']
        instance_id = self._instance_id(vnf_dict)
        create_failed = False

        try:
            self._vnf_manager.invoke(
                driver_name, 'create_wait', plugin=self, context=context,
                vnf_dict=vnf_dict, vnf_id=instance_id,
                auth_attr=auth_attr)
        except vnfm.VNFCreateWaitFailed as e:
            LOG.error(_LE("VNF Create failed for vnf_id %s"), vnf_id)
            create_failed = True
            vnf_dict['status'] = constants.ERROR
            self.set_vnf_error_status_reason(context, vnf_id,
                                             six.text_type(e))

        if instance_id is None or create_failed:
            mgmt_url = None
        else:
            # mgmt_url = self.mgmt_url(context, vnf_dict)
            # FIXME(yamahata):
            mgmt_url = vnf_dict['mgmt_url']

        self._create_vnf_post(
            context, vnf_id, instance_id, mgmt_url, vnf_dict)
        self.mgmt_create_post(context, vnf_dict)

        if instance_id is None or create_failed:
            return

        vnf_dict['mgmt_url'] = mgmt_url

        kwargs = {
            mgmt_constants.KEY_ACTION: mgmt_constants.ACTION_CREATE_VNF,
            mgmt_constants.KEY_KWARGS: {'vnf': vnf_dict},
        }
        new_status = constants.ACTIVE
        try:
            self.mgmt_call(context, vnf_dict, kwargs)
        except exceptions.MgmtDriverException:
            LOG.error(_('VNF configuration failed'))
            new_status = constants.ERROR
            self.set_vnf_error_status_reason(context, vnf_id,
            'Unable to configure VDU')
        vnf_dict['status'] = new_status
        self._create_vnf_status(context, vnf_id, new_status)

    def get_vim(self, context, vnf):
        region_name = vnf.setdefault('placement_attr', {}).get(
            'region_name', None)
        vim_res = self.vim_client.get_vim(context, vnf['vim_id'],
                                          region_name)
        vnf['placement_attr']['vim_name'] = vim_res['vim_name']
        vnf['vim_id'] = vim_res['vim_id']
        return vim_res['vim_auth']

    def _create_vnf(self, context, vnf, vim_auth):
        vnf_dict = self._create_vnf_pre(
            context, vnf) if not vnf.get('id') else vnf
        vnf_id = vnf_dict['id']
        driver_name = self._infra_driver_name(vnf_dict)
        LOG.debug(_('vnf_dict %s'), vnf_dict)
        self.mgmt_create_pre(context, vnf_dict)
        try:
            instance_id = self._vnf_manager.invoke(
                driver_name, 'create', plugin=self,
                context=context, vnf=vnf_dict, auth_attr=vim_auth)
        except Exception:
            with excutils.save_and_reraise_exception():
                self.delete_vnf(context, vnf_id)

        if instance_id is None:
            self._create_vnf_post(context, vnf_id, None, None,
                                  vnf_dict)
            return
        vnf_dict['instance_id'] = instance_id
        return vnf_dict

    def create_vnf(self, context, vnf):
        vnf_info = vnf['vnf']
        vnf_attributes = vnf_info['attributes']
        if vnf_attributes.get('param_values'):
            param = vnf_attributes['param_values']
            if isinstance(param, dict):
                # TODO(sripriya) remove this yaml dump once db supports storing
                # json format of yaml files in a separate column instead of
                #  key value string pairs in vnf attributes table
                vnf_attributes['param_values'] = yaml.safe_dump(param)
            else:
                self._report_deprecated_yaml_str()
        if vnf_attributes.get('config'):
            config = vnf_attributes['config']
            if isinstance(config, dict):
                # TODO(sripriya) remove this yaml dump once db supports storing
                # json format of yaml files in a separate column instead of
                #  key value string pairs in vnf attributes table
                vnf_attributes['config'] = yaml.safe_dump(config)
            else:
                self._report_deprecated_yaml_str()
        vim_auth = self.get_vim(context, vnf_info)
        vnf_dict = self._create_vnf(context, vnf_info, vim_auth)

        def create_vnf_wait():
            self._create_vnf_wait(context, vnf_dict, vim_auth)
            self.add_vnf_to_monitor(vnf_dict, vim_auth)
            self.config_vnf(context, vnf_dict)
        self.spawn_n(create_vnf_wait)
        return vnf_dict

    # not for wsgi, but for service to create hosting vnf
    # the vnf is NOT added to monitor.
    def create_vnf_sync(self, context, vnf):
        vim_auth = self.get_vim(context, vnf)
        vnf_dict = self._create_vnf(context, vnf, vim_auth)
        self._create_vnf_wait(context, vnf_dict, vim_auth)
        return vnf_dict

    def _update_vnf_wait(self, context, vnf_dict, vim_auth):
        driver_name = self._infra_driver_name(vnf_dict)
        instance_id = self._instance_id(vnf_dict)
        kwargs = {
            mgmt_constants.KEY_ACTION: mgmt_constants.ACTION_UPDATE_VNF,
            mgmt_constants.KEY_KWARGS: {'vnf': vnf_dict},
        }
        new_status = constants.ACTIVE
        placement_attr = vnf_dict['placement_attr']
        region_name = placement_attr.get('region_name')

        try:
            self._vnf_manager.invoke(
                driver_name, 'update_wait', plugin=self,
                context=context, vnf_id=instance_id, auth_attr=vim_auth,
                region_name=region_name)
            self.mgmt_call(context, vnf_dict, kwargs)
        except exceptions.MgmtDriverException as e:
            LOG.error(_('VNF configuration failed'))
            new_status = constants.ERROR
            self.set_vnf_error_status_reason(context, vnf_dict['id'],
                                             six.text_type(e))
        vnf_dict['status'] = new_status
        self.mgmt_update_post(context, vnf_dict)

        self._update_vnf_post(context, vnf_dict['id'],
                              new_status, vnf_dict)

    def update_vnf(self, context, vnf_id, vnf):
        vnf_attributes = vnf['vnf']['attributes']
        if vnf_attributes.get('config'):
            config = vnf_attributes['config']
            if isinstance(config, dict):
                # TODO(sripriya) remove this yaml dump once db supports storing
                # json format of yaml files in a separate column instead of
                #  key value string pairs in vnf attributes table
                vnf_attributes['config'] = yaml.safe_dump(config)
            else:
                self._report_deprecated_yaml_str()
        vnf_dict = self._update_vnf_pre(context, vnf_id)
        vim_auth = self.get_vim(context, vnf_dict)
        driver_name = self._infra_driver_name(vnf_dict)
        instance_id = self._instance_id(vnf_dict)

        try:
            self.mgmt_update_pre(context, vnf_dict)
            self._vnf_manager.invoke(
                driver_name, 'update', plugin=self, context=context,
                vnf_id=instance_id, vnf_dict=vnf_dict,
                vnf=vnf, auth_attr=vim_auth)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                vnf_dict['status'] = constants.ERROR
                self.set_vnf_error_status_reason(context,
                                                 vnf_dict['id'],
                                                 six.text_type(e))
                self.mgmt_update_post(context, vnf_dict)
                self._update_vnf_post(context, vnf_id, constants.ERROR)

        self.spawn_n(self._update_vnf_wait, context, vnf_dict, vim_auth)
        return vnf_dict

    def _delete_vnf_wait(self, context, vnf_dict, auth_attr):
        driver_name = self._infra_driver_name(vnf_dict)
        instance_id = self._instance_id(vnf_dict)
        e = None
        if instance_id:
            placement_attr = vnf_dict['placement_attr']
            region_name = placement_attr.get('region_name')
            try:
                self._vnf_manager.invoke(
                    driver_name,
                    'delete_wait',
                    plugin=self,
                    context=context,
                    vnf_id=instance_id,
                    auth_attr=auth_attr,
                    region_name=region_name)
            except Exception as e_:
                e = e_
                vnf_dict['status'] = constants.ERROR
                vnf_dict['error_reason'] = six.text_type(e)
                LOG.exception(_('_delete_vnf_wait'))

        self.mgmt_delete_post(context, vnf_dict)
        vnf_id = vnf_dict['id']
        self._delete_vnf_post(context, vnf_id, e)

    def delete_vnf(self, context, vnf_id):
        vnf_dict = self._delete_vnf_pre(context, vnf_id)
        vim_auth = self.get_vim(context, vnf_dict)
        self._vnf_monitor.delete_hosting_vnf(vnf_id)
        driver_name = self._infra_driver_name(vnf_dict)
        instance_id = self._instance_id(vnf_dict)
        placement_attr = vnf_dict['placement_attr']
        region_name = placement_attr.get('region_name')
        kwargs = {
            mgmt_constants.KEY_ACTION: mgmt_constants.ACTION_DELETE_VNF,
            mgmt_constants.KEY_KWARGS: {'vnf': vnf_dict},
        }
        try:
            self.mgmt_delete_pre(context, vnf_dict)
            self.mgmt_call(context, vnf_dict, kwargs)
            if instance_id:
                self._vnf_manager.invoke(driver_name,
                                         'delete',
                                         plugin=self,
                                         context=context,
                                         vnf_id=instance_id,
                                         auth_attr=vim_auth,
                                         region_name=region_name)
        except Exception as e:
            # TODO(yamahata): when the devaice is already deleted. mask
            # the error, and delete row in db
            # Other case mark error
            with excutils.save_and_reraise_exception():
                vnf_dict['status'] = constants.ERROR
                vnf_dict['error_reason'] = six.text_type(e)
                self.mgmt_delete_post(context, vnf_dict)
                self._delete_vnf_post(context, vnf_id, e)

        self.spawn_n(self._delete_vnf_wait, context, vnf_dict, vim_auth)

    def _handle_vnf_scaling(self, context, policy):
        # validate
        def _validate_scaling_policy():
            type = policy['type']

            if type not in constants.POLICY_ACTIONS.keys():
                raise exceptions.VnfPolicyTypeInvalid(
                    type=type,
                    valid_types=constants.POLICY_ACTIONS.keys(),
                    policy=policy['id']
                )
            action = policy['action']

            if action not in constants.POLICY_ACTIONS[type]:
                raise exceptions.VnfPolicyActionInvalid(
                    action=action,
                    valid_actions=constants.POLICY_ACTIONS[type],
                    policy=policy['id']
                )

            LOG.debug(_("Policy %s is validated successfully") % policy)

        def _get_status():
            if policy['action'] == constants.ACTION_SCALE_IN:
                status = constants.PENDING_SCALE_IN
            else:
                status = constants.PENDING_SCALE_OUT

            return status

        # pre
        def _handle_vnf_scaling_pre():
            status = _get_status()
            result = self._update_vnf_scaling_status(context,
                                                     policy,
                                                     [constants.ACTIVE],
                                                     status)
            LOG.debug(_("Policy %(policy)s vnf is at %(status)s"),
                      {'policy': policy,
                       'status': status})
            return result

        # post
        def _handle_vnf_scaling_post(new_status, mgmt_url=None):
            status = _get_status()
            result = self._update_vnf_scaling_status(context,
                                                     policy,
                                                     [status],
                                                     new_status,
                                                     mgmt_url)
            LOG.debug(_("Policy %(policy)s vnf is at %(status)s"),
                      {'policy': policy,
                       'status': new_status})
            return result

        # action
        def _vnf_policy_action():
            try:
                self._vnf_manager.invoke(
                    infra_driver,
                    'scale',
                    plugin=self,
                    context=context,
                    auth_attr=vim_auth,
                    policy=policy,
                    region_name=region_name
                )
                LOG.debug(_("Policy %s action is started successfully") %
                          policy)
            except Exception as e:
                LOG.error(_("Policy %s action is failed to start") %
                          policy)
                with excutils.save_and_reraise_exception():
                    vnf['status'] = constants.ERROR
                    self.set_vnf_error_status_reason(
                        context,
                        policy['vnf_id'],
                        six.text_type(e))
                    _handle_vnf_scaling_post(constants.ERROR)

        # wait
        def _vnf_policy_action_wait():
            try:
                LOG.debug(_("Policy %s action is in progress") %
                          policy)
                mgmt_url = self._vnf_manager.invoke(
                    infra_driver,
                    'scale_wait',
                    plugin=self,
                    context=context,
                    auth_attr=vim_auth,
                    policy=policy,
                    region_name=region_name
                )
                LOG.debug(_("Policy %s action is completed successfully") %
                          policy)
                _handle_vnf_scaling_post(constants.ACTIVE, mgmt_url)
                # TODO(kanagaraj-manickam): Add support for config and mgmt
            except Exception as e:
                LOG.error(_("Policy %s action is failed to complete") %
                          policy)
                with excutils.save_and_reraise_exception():
                    self.set_vnf_error_status_reason(
                        context,
                        policy['vnf_id'],
                        six.text_type(e))
                    _handle_vnf_scaling_post(constants.ERROR)

        _validate_scaling_policy()

        vnf = _handle_vnf_scaling_pre()
        policy['instance_id'] = vnf['instance_id']

        infra_driver = self._infra_driver_name(vnf)
        vim_auth = self.get_vim(context, vnf)
        region_name = vnf.get('placement_attr', {}).get('region_name', None)
        _vnf_policy_action()
        self.spawn_n(_vnf_policy_action_wait)

        return policy

    def _report_deprecated_yaml_str(self):
        utils.deprecate_warning(what='yaml as string',
                                as_of='N', in_favor_of='yaml as dictionary')

    def _make_policy_dict(self, vnf, name, policy):
        p = {}
        p['type'] = policy['type']
        p['properties'] = policy['properties']
        p['vnf'] = vnf
        p['name'] = name
        p['id'] = p['name']
        return p

    def get_vnf_policies(
            self, context, vnf_id, filters=None, fields=None):
        vnf = self.get_vnf(context, vnf_id)
        vnfd_tmpl = yaml.load(vnf['vnfd']['attributes']['vnfd'])
        policy_list = []

        if vnfd_tmpl.get('tosca_definitions_version'):
            polices = vnfd_tmpl['topology_template'].get('policies', [])
            for policy_dict in polices:
                for name, policy in policy_dict.items():
                    def _add(policy):
                        p = self._make_policy_dict(vnf, name, policy)
                        p['name'] = name
                        policy_list.append(p)

                    # Check for filters
                    if filters.get('name'):
                        if name == filters.get('name'):
                            _add(policy)
                            break
                        else:
                            continue

                    _add(policy)

        return policy_list

    def get_vnf_policy(
            self, context, policy_id, vnf_id, fields=None):
        policies = self.get_vnf_policies(context,
                                         vnf_id,
                                         filters={'name': policy_id})
        if policies:
            return policies[0]

        raise exceptions.VnfPolicyNotFound(policy=policy_id,
                                           vnf_id=vnf_id)

    def create_vnf_scale(self, context, vnf_id, scale):
        policy_ = self.get_vnf_policy(context,
                                      scale['scale']['policy'],
                                      vnf_id)
        policy_.update({'action': scale['scale']['type']})
        self._handle_vnf_scaling(context, policy_)

        return scale['scale']
