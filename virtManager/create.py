#
# Copyright (C) 2008, 2013, 2014, 2015 Red Hat, Inc.
# Copyright (C) 2008 Cole Robinson <crobinso@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#

import logging
import threading
import time

from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import Pango

import virtinst
from virtinst import util

from . import uiutil
from .baseclass import vmmGObjectUI
from .asyncjob import vmmAsyncJob
from .storagebrowse import vmmStorageBrowser
from .details import vmmDetails
from .domain import vmmDomainVirtinst
from .netlist import vmmNetworkList
from .mediacombo import vmmMediaCombo
from .addstorage import vmmAddStorage

# Number of seconds to wait for media detection
DETECT_TIMEOUT = 20
DETECT_INPROGRESS = -1
DETECT_FAILED = -2

DEFAULT_MEM = 1024

PAGE_NAME = 0
PAGE_INSTALL = 1
PAGE_MEM = 2
PAGE_STORAGE = 3
PAGE_FINISH = 4

INSTALL_PAGE_ISO = 0
INSTALL_PAGE_URL = 1
INSTALL_PAGE_PXE = 2
INSTALL_PAGE_IMPORT = 3
INSTALL_PAGE_CONTAINER_APP = 4
INSTALL_PAGE_CONTAINER_OS = 5


def pretty_arch(_a):
    if _a == "armv7l":
        return "arm"
    return _a


class vmmCreate(vmmGObjectUI):
    __gsignals__ = {
        "action-show-domain": (GObject.SignalFlags.RUN_FIRST, None, [str, str]),
    }

    def __init__(self, engine):
        vmmGObjectUI.__init__(self, "create.ui", "vmm-create")
        self.engine = engine

        self.conn = None
        self.capsinfo = None

        self.guest = None
        self.disk = None
        self.nic = None

        self.storage_browser = None

        # Distro detection state variables
        self.detectedDistro = None
        self.mediaDetected = False
        self.show_all_os = False

        # 'Guest' class from the previous failed install
        self.failed_guest = None

        # Whether there was an error at dialog startup
        self.have_startup_error = False

        # 'Configure before install' window
        self.config_window = None
        self.config_window_signals = []

        self.netlist = None
        self.mediacombo = None

        self.addstorage = vmmAddStorage(self.conn, self.builder, self.topwin)
        self.widget("config-storage-align").add(self.addstorage.top_box)
        self.addstorage.connect("browse-clicked", self._browse_file_cb)

        self.builder.connect_signals({
            "on_vmm_newcreate_delete_event" : self._close_requested,

            "on_create_cancel_clicked": self._close_requested,
            "on_create_back_clicked" : self.back,
            "on_create_forward_clicked" : self.forward,
            "on_create_finish_clicked" : self.finish,
            "on_create_pages_switch_page": self.page_changed,

            "on_create_vm_name_activate": self.forward,
            "on_create_conn_changed": self.conn_changed,
            "on_method_changed": self.method_changed,
            "on_config_machine_changed": self.machine_changed,

            "on_install_url_box_changed": self.url_box_changed,
            "on_install_local_cdrom_toggled": self.toggle_local_cdrom,
            "on_install_local_cdrom_combo_changed": self.detect_media_os,
            "on_install_local_box_changed": self.local_box_changed,
            "on_install_local_browse_clicked": self.browse_iso,
            "on_install_import_browse_clicked": self.browse_import,
            "on_install_app_browse_clicked": self.browse_app,
            "on_install_oscontainer_browse_clicked": self.browse_oscontainer,

            "on_install_detect_os_toggled": self.toggle_detect_os,
            "on_install_os_type_changed": self.change_os_type,
            "on_install_os_version_changed": self.change_os_version,
            "on_install_local_iso_toggled": self.toggle_local_iso,
            "on_install_detect_os_box_show": self.detect_visibility_changed,
            "on_install_detect_os_box_hide": self.detect_visibility_changed,

            "on_config_kernel_browse_clicked": self.browse_kernel,
            "on_config_initrd_browse_clicked": self.browse_initrd,
            "on_config_dtb_browse_clicked": self.browse_dtb,

            "on_enable_storage_toggled": self.toggle_enable_storage,

            "on_config_hv_changed": self.hv_changed,
            "on_config_arch_changed": self.arch_changed,
        })
        self.bind_escape_key_close()

        self.set_initial_state()

    def is_visible(self):
        return self.topwin.get_visible()

    def show(self, parent, uri=None):
        logging.debug("Showing new vm wizard")

        if not self.is_visible():
            self.reset_state(uri)
            self.topwin.set_transient_for(parent)

        self.topwin.present()

    def _close(self, ignore1=None, ignore2=None):
        if self.is_visible():
            logging.debug("Closing new vm wizard")
        self.topwin.hide()

        if self.config_window:
            self.config_window.close()
        if self.storage_browser:
            self.storage_browser.close()

    def _cleanup(self):
        self.remove_conn()

        self.conn = None
        self.capsinfo = None

        self.guest = None
        self.disk = None
        self.nic = None

        if self.storage_browser:
            self.storage_browser.cleanup()
            self.storage_browser = None
        if self.netlist:
            self.netlist.cleanup()
            self.netlist = None

        if self.netlist:
            self.netlist.cleanup()
            self.netlist = None
        if self.mediacombo:
            self.mediacombo.cleanup()
            self.mediacombo = None
        if self.addstorage:
            self.addstorage.cleanup()
            self.addstorage = None

    def remove_conn(self):
        self.conn = None
        self.capsinfo = None

    def set_conn(self, newconn, force_validate=False):
        if self.conn == newconn and not force_validate:
            return

        self.remove_conn()
        self.conn = newconn
        if self.conn:
            self.set_conn_state()


    # State init methods
    def startup_error(self, error, hideinstall=True):
        self.have_startup_error = True
        self.widget("startup-error-box").show()
        self.widget("create-forward").set_sensitive(False)
        if hideinstall:
            self.widget("install-box").hide()
            self.widget("arch-expander").hide()

        self.widget("startup-error").set_text("Error: %s" % error)
        return False

    def startup_warning(self, error):
        self.widget("startup-error-box").show()
        self.widget("startup-error").set_text("Warning: %s" % error)

    def set_initial_state(self):
        self.widget("create-pages").set_show_tabs(False)
        self.widget("install-method-pages").set_show_tabs(False)

        blue = Gdk.Color.parse("#0072A8")[1]
        self.widget("header").modify_bg(Gtk.StateType.NORMAL, blue)

        # Connection list
        self.widget("create-conn-label").set_text("")
        self.widget("startup-error").set_text("")
        conn_list = self.widget("create-conn")
        conn_model = Gtk.ListStore(str, str)
        conn_list.set_model(conn_model)
        text = uiutil.init_combo_text_column(conn_list, 1)
        text.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)

        # ISO media list
        iso_list = self.widget("install-local-box")
        iso_model = Gtk.ListStore(str)
        iso_list.set_model(iso_model)
        iso_list.set_entry_text_column(0)
        self.widget("install-local-box").get_child().connect("activate",
                                                    self.detect_media_os)

        # Lists for the install urls
        media_url_list = self.widget("install-url-box")
        media_url_model = Gtk.ListStore(str)
        media_url_list.set_model(media_url_model)
        media_url_list.set_entry_text_column(0)
        self.widget("install-url-box").get_child().connect("activate",
                                                    self.detect_media_os)

        ks_url_list = self.widget("install-ks-box")
        ks_url_model = Gtk.ListStore(str)
        ks_url_list.set_model(ks_url_model)
        ks_url_list.set_entry_text_column(0)

        def sep_func(model, it, combo):
            ignore = combo
            return model[it][2]

        # Lists for distro type + variant
        # [os value, os label, is seperator, is 'show all'
        os_type_list = self.widget("install-os-type")
        os_type_model = Gtk.ListStore(str, str, bool, bool)
        os_type_list.set_model(os_type_model)
        uiutil.init_combo_text_column(os_type_list, 1)
        os_type_list.set_row_separator_func(sep_func, os_type_list)

        os_variant_list = self.widget("install-os-version")
        os_variant_model = Gtk.ListStore(str, str, bool, bool)
        os_variant_list.set_model(os_variant_model)
        uiutil.init_combo_text_column(os_variant_list, 1)
        os_variant_list.set_row_separator_func(sep_func, os_variant_list)

        entry = self.widget("install-os-version-entry")
        completion = Gtk.EntryCompletion()
        entry.set_completion(completion)
        completion.set_text_column(1)
        completion.set_inline_completion(True)
        completion.set_model(os_variant_model)

        # Archtecture
        # [value, label]
        archList = self.widget("config-arch")
        archModel = Gtk.ListStore(str, str)
        archList.set_model(archModel)
        uiutil.init_combo_text_column(archList, 1)
        archList.set_row_separator_func(
            lambda m, i, ignore: m[i][0] is None, None)

        hyperList = self.widget("config-hv")
        hyperModel = Gtk.ListStore(str, str)
        hyperList.set_model(hyperModel)
        uiutil.init_combo_text_column(hyperList, 0)

        lst = self.widget("config-machine")
        model = Gtk.ListStore(str)
        lst.set_model(model)
        uiutil.init_combo_text_column(lst, 0)
        lst.set_row_separator_func(lambda m, i, ignore: m[i][0] is None, None)

    def reset_state(self, urihint=None):
        self.failed_guest = None
        self.have_startup_error = False
        self.guest = None
        self.disk = None
        self.nic = None
        self.show_all_os = False

        self.widget("create-pages").set_current_page(PAGE_NAME)
        self.page_changed(None, None, PAGE_NAME)

        # Name page state
        self.widget("create-vm-name").set_text("")
        self.widget("method-local").set_active(True)
        self.widget("create-conn").set_active(-1)
        activeconn = self.populate_conn_list(urihint)
        self.widget("arch-expander").set_expanded(False)

        try:
            self.set_conn(activeconn, force_validate=True)
        except Exception, e:
            logging.exception("Error setting create wizard conn state.")
            return self.startup_error(str(e))

        if not activeconn:
            return self.startup_error(
                                _("No active connection to install on."))

        # Everything from this point forward should be connection independent

        # Distro/Variant
        self.toggle_detect_os(self.widget("install-detect-os"))
        self.populate_os_type_model()
        self.widget("install-os-type").set_active(0)

        self.widget("install-local-box").get_child().set_text("")
        iso_model = self.widget("install-local-box").get_model()
        self.populate_media_model(iso_model, self.config.get_iso_paths())

        # Install URL
        self.widget("install-urlopts-entry").set_text("")
        self.widget("install-ks-box").get_child().set_text("")
        self.widget("install-url-box").get_child().set_text("")
        self.widget("install-url-options").set_expanded(False)
        urlmodel = self.widget("install-url-box").get_model()
        ksmodel  = self.widget("install-ks-box").get_model()
        self.populate_media_model(urlmodel, self.config.get_media_urls())
        self.populate_media_model(ksmodel, self.config.get_kickstart_urls())
        self.set_distro_labels("-", "-", force=True)

        # Install import
        self.widget("install-import-entry").set_text("")
        self.widget("config-kernel").set_text("")
        self.widget("config-initrd").set_text("")
        self.widget("config-dtb").set_text("")

        # Install container app
        self.widget("install-app-entry").set_text("/bin/sh")

        # Install container OS
        self.widget("install-oscontainer-fs").set_text("")

        # Storage
        self.widget("enable-storage").set_active(True)
        self.addstorage.reset_state()
        self.addstorage.widget("config-storage-create").set_active(True)
        self.addstorage.widget("config-storage-entry").set_text("")
        self.addstorage.widget("config-storage-nosparse").set_active(True)

        fmt = self.conn.get_default_storage_format()
        can_alloc = fmt in ["raw"]
        self.addstorage.widget("config-storage-nosparse").set_active(can_alloc)
        self.addstorage.widget("config-storage-nosparse").set_sensitive(can_alloc)
        self.addstorage.widget("config-storage-nosparse").set_tooltip_text(
            not can_alloc and
            (_("Disk format '%s' does not support full allocation.") % fmt) or
            "")

        # Final page
        self.widget("summary-customize").set_active(False)

        # Make sure window is a sane size
        self.topwin.resize(1, 1)

    def set_caps_state(self):
        # State that is dependent on when capsinfo changes

        # Helper state
        is_local = not self.conn.is_remote()
        is_storage_capable = self.conn.is_storage_capable()
        can_storage = (is_local or is_storage_capable)
        is_pv = (self.capsinfo.os_type == "xen")
        is_container = self.conn.is_container()
        can_remote_url = self.conn.get_backend().support_remote_url_install()

        installable_arch = (self.capsinfo.arch in
            ["i686", "x86_64", "ppc64", "ppc64le", "ia64", "s390x"])

        if self.capsinfo.arch == "aarch64":
            try:
                guest = self.conn.caps.build_virtinst_guest(self.capsinfo)
                guest.set_uefi_default()
                installable_arch = True
                logging.debug("UEFI found for aarch64, setting it as default.")
            except Exception, e:
                installable_arch = False
                logging.debug("Error checking for aarch64 UEFI default",
                    exc_info=True)
                msg = _("Failed to setup UEFI for AArch64: %s\n"
                        "Install options are limited.") % e
                self.startup_warning(msg)

        # Install Options
        method_tree = self.widget("method-tree")
        method_pxe = self.widget("method-pxe")
        method_local = self.widget("method-local")
        method_import = self.widget("method-import")
        method_container_app = self.widget("method-container-app")

        method_tree.set_sensitive((is_local or can_remote_url) and
                                  installable_arch)
        method_local.set_sensitive(not is_pv and can_storage and
                                   installable_arch)
        method_pxe.set_sensitive(not is_pv and installable_arch)
        method_import.set_sensitive(can_storage)
        virt_methods = [method_local, method_tree, method_pxe, method_import]

        pxe_tt = None
        local_tt = None
        tree_tt = None
        import_tt = None

        if not is_local:
            if not can_remote_url:
                tree_tt = _("Libvirt version does not "
                            "support remote URL installs.")
            if not is_storage_capable:
                local_tt = _("Connection does not support storage management.")
                import_tt = local_tt

        if is_pv:
            base = _("%s installs not available for paravirt guests.")
            pxe_tt = base % "PXE"
            local_tt = base % "CDROM/ISO"

        if not installable_arch:
            msg = (_("Architecture '%s' is not installable") %
                   self.capsinfo.arch)
            tree_tt = msg
            local_tt = msg
            pxe_tt = msg

        if not any([w.get_active() and w.get_sensitive()
                    for w in virt_methods]):
            for w in virt_methods:
                if w.get_sensitive():
                    w.set_active(True)
                    break

        if not (is_container or
                [w for w in virt_methods if w.get_sensitive()]):
            return self.startup_error(
                    _("No install methods available for this connection."),
                    hideinstall=False)

        method_tree.set_tooltip_text(tree_tt or "")
        method_local.set_tooltip_text(local_tt or "")
        method_pxe.set_tooltip_text(pxe_tt or "")
        method_import.set_tooltip_text(import_tt or "")

        # Container install options
        method_container_app.set_active(True)
        self.widget("virt-install-box").set_visible(not is_container)
        self.widget("container-install-box").set_visible(is_container)

        show_dtb = ("arm" in self.capsinfo.arch or
                    "microblaze" in self.capsinfo.arch or
                    "ppc" in self.capsinfo.arch)
        self.widget("config-kernel-box").set_visible(not installable_arch)
        uiutil.set_grid_row_visible(self.widget("config-dtb"), show_dtb)

    def set_conn_state(self):
        # Update all state that has some dependency on the current connection
        self.conn.schedule_priority_tick(pollnet=True,
                                         pollpool=True, polliface=True,
                                         pollnodedev=True)

        self.widget("install-box").show()
        self.widget("startup-error-box").hide()
        self.widget("create-forward").set_sensitive(True)

        self.conn.invalidate_caps()
        self.change_caps()

        if not self.capsinfo.guest.has_install_options():
            error = _("No hypervisor options were found for this "
                      "connection.")

            if self.conn.is_qemu():
                error += "\n\n"
                error += _("This usually means that QEMU or KVM is not "
                           "installed on your machine, or the KVM kernel "
                           "modules are not loaded.")
            return self.startup_error(error)

        # A bit out of order, but populate arch + hv lists so we can
        # determine a default
        self.populate_hv()
        self.populate_arch()

        show_arch = (self.widget("config-hv").get_visible() or
                     self.widget("config-arch").get_visible() or
                     self.widget("config-machine").get_visible())
        uiutil.set_grid_row_visible(self.widget("arch-expander"), show_arch)

        if self.conn.is_xen():
            has_hvm_guests = False
            for g in self.conn.caps.guests:
                if g.os_type == "hvm":
                    has_hvm_guests = True

            if not has_hvm_guests:
                error = _("Host is not advertising support for full "
                          "virtualization. Install options may be limited.")
                self.startup_warning(error)

        elif self.conn.is_qemu():
            if not self.capsinfo.guest.is_kvm_available():
                error = _("KVM is not available. This may mean the KVM "
                 "package is not installed, or the KVM kernel modules "
                 "are not loaded. Your virtual machines may perform poorly.")
                self.startup_warning(error)

        # Install local
        iso_option = self.widget("install-local-iso")
        cdrom_option = self.widget("install-local-cdrom")

        if self.mediacombo:
            self.widget("install-local-cdrom-align").remove(
                self.mediacombo.top_box)
            self.mediacombo.cleanup()
            self.mediacombo = None

        self.mediacombo = vmmMediaCombo(self.conn, self.builder, self.topwin,
                                        vmmMediaCombo.MEDIA_CDROM)
        def mediacombo_changed(src):
            ignore = src
            self.mediaDetected = False
            self.detect_media_os()
        self.mediacombo.combo.connect("changed", mediacombo_changed)
        self.mediacombo.reset_state()
        self.widget("install-local-cdrom-align").add(
            self.mediacombo.top_box)

        # Don't select physical CDROM if no valid media is present
        cdrom_option.set_active(self.mediacombo.has_media())
        iso_option.set_active(not self.mediacombo.has_media())

        enable_phys = not self._stable_defaults()
        cdrom_option.set_sensitive(enable_phys)
        cdrom_option.set_tooltip_text("" if enable_phys else
            _("Physical CDROM passthrough not supported with this hypervisor"))

        # Only allow ISO option for remote VM
        is_local = not self.conn.is_remote()
        if not is_local or not enable_phys:
            iso_option.set_active(True)

        self.toggle_local_cdrom(cdrom_option)
        self.toggle_local_iso(iso_option)

        # Memory
        memory = int(self.conn.host_memory_size())
        mem_label = (_("Up to %(maxmem)s available on the host") %
                     {'maxmem': self.pretty_memory(memory)})
        mem_label = ("<span size='small' color='#484848'>%s</span>" %
                     mem_label)
        self.widget("config-mem").set_range(50, memory / 1024)
        self.widget("phys-mem-label").set_markup(mem_label)

        # CPU
        phys_cpus = int(self.conn.host_active_processor_count())
        cmax = phys_cpus
        if cmax <= 0:
            cmax = 1
        cpu_label = (_("Up to %(numcpus)d available") %
                     {'numcpus': int(phys_cpus)})
        cpu_label = ("<span size='small' color='#484848'>%s</span>" %
                     cpu_label)
        self.widget("config-cpus").set_range(1, cmax)
        self.widget("phys-cpu-label").set_markup(cpu_label)

        # Storage
        self.addstorage.conn = self.conn
        self.addstorage.reset_state()

        # Networking
        self.widget("config-advanced-expander").set_expanded(False)

        if self.netlist:
            self.widget("config-netdev-ui-align").remove(self.netlist.top_box)
            self.netlist.cleanup()
            self.netlist = None

        self.netlist = vmmNetworkList(self.conn, self.builder, self.topwin)
        self.widget("config-netdev-ui-align").add(self.netlist.top_box)
        self.netlist.connect("changed", self.netdev_changed)
        self.netlist.reset_state()

    def populate_hv(self):
        hv_list = self.widget("config-hv")
        model = hv_list.get_model()
        model.clear()

        default = 0
        guests = self.conn.caps.guests[:]
        if not (self.conn.is_xen() or self.conn.is_test_conn()):
            guests = []

        for guest in self.conn.caps.guests:
            gtype = guest.os_type
            if not guest.domains:
                continue
            dom = guest.domains[0]
            domtype = dom.hypervisor_type
            label = self.conn.pretty_hv(gtype, domtype)

            # Don't add multiple rows for each arch
            for m in model:
                if m[0] == label:
                    label = None
                    break
            if label is None:
                continue

            # Determine if this is the default given by guest_lookup
            if (gtype == self.capsinfo.os_type and
                self.capsinfo.hypervisor_type == domtype):
                default = len(model)

            model.append([label, gtype])

        show = bool(guests)
        uiutil.set_grid_row_visible(hv_list, show)
        if show:
            hv_list.set_active(default)

    def populate_arch(self):
        arch_list = self.widget("config-arch")
        model = arch_list.get_model()
        model.clear()

        default = 0
        archs = []
        for guest in self.conn.caps.guests:
            if guest.os_type == self.capsinfo.os_type:
                archs.append(guest.arch)

        # Combine x86/i686 to avoid confusion
        if (self.conn.caps.host.cpu.arch == "x86_64" and
            "x86_64" in archs and "i686" in archs):
            archs.remove("i686")
        archs.sort()

        prios = ["x86_64", "i686", "aarch64", "armv7l", "ppc64", "ppc64le",
            "s390x"]
        if self.conn.caps.host.cpu.arch not in prios:
            prios = []
        else:
            for p in prios[:]:
                if p not in archs:
                    prios.remove(p)
                else:
                    archs.remove(p)
        if prios:
            if archs:
                prios += [None]
            archs = prios + archs

        default = 0
        if self.capsinfo.arch in archs:
            default = archs.index(self.capsinfo.arch)

        for arch in archs:
            model.append([arch, pretty_arch(arch)])

        show = not (len(archs) < 2)
        uiutil.set_grid_row_visible(arch_list, show)
        arch_list.set_active(default)

    def populate_machine(self):
        lst = self.widget("config-machine")
        model = lst.get_model()
        model.clear()

        machines = self.capsinfo.machines[:]
        if self.capsinfo.arch in ["i686", "x86_64"]:
            machines = []
        machines.sort()

        defmachine = None
        prios = []
        recommended_machine = self.capsinfo.get_recommended_machine()
        if recommended_machine:
            defmachine = recommended_machine
            prios = [defmachine]

        for p in prios[:]:
            if p not in machines:
                prios.remove(p)
            else:
                machines.remove(p)
        if prios:
            machines = prios + [None] + machines

        default = 0
        if defmachine and defmachine in machines:
            default = machines.index(defmachine)

        for m in machines:
            model.append([m])

        show = (len(machines) > 1)
        uiutil.set_grid_row_visible(lst, show)
        if show:
            lst.set_active(default)
        else:
            lst.emit("changed")

    def populate_conn_list(self, urihint=None):
        conn_list = self.widget("create-conn")
        model = conn_list.get_model()
        model.clear()

        default = -1
        for c in self.engine.conns.values():
            connobj = c["conn"]
            if not connobj.is_active():
                continue

            if connobj.get_uri() == urihint:
                default = len(model)
            elif default < 0 and not connobj.is_remote():
                # Favor local connections over remote connections
                default = len(model)

            model.append([connobj.get_uri(), connobj.get_pretty_desc()])

        no_conns = (len(model) == 0)

        if default < 0 and not no_conns:
            default = 0

        activeuri = ""
        activedesc = ""
        activeconn = None
        if not no_conns:
            conn_list.set_active(default)
            activeuri, activedesc = model[default]
            activeconn = self.engine.conns[activeuri]["conn"]

        self.widget("create-conn-label").set_text(activedesc)
        if len(model) <= 1:
            self.widget("create-conn").hide()
            self.widget("create-conn-label").show()
        else:
            self.widget("create-conn").show()
            self.widget("create-conn-label").hide()

        return activeconn

    def _add_os_row(self, model, name="", label="", supported=False,
                      sep=False, action=False):
        visible = self.show_all_os or supported
        if sep or action:
            visible = not self.show_all_os

        if not visible:
            return

        model.append([name, label, sep, action])

    def _fill_os_type_model(self, model):
        model.clear()

        # Kind of a hack, just show linux + windows by default since
        # that's all 98% of people care about
        supportl = ["generic", "linux", "windows"]

        # Move 'generic' to the front of the list
        types = virtinst.OSDB.list_types()
        types.remove("generic")
        types.insert(0, "generic")

        for typename in types:
            supported = (typename in supportl)
            typelabel = typename.capitalize()
            if typename in ["unix"]:
                typelabel = typename.upper()

            self._add_os_row(model, typename, typelabel, supported)

        self._add_os_row(model, sep=True)
        self._add_os_row(model, label=_("Show all OS options"), action=True)

    def populate_os_type_model(self):
        widget = self.widget("install-os-type")
        model = widget.get_model()
        self._fill_os_type_model(model)

        # Select 'generic' by default
        widget.set_active(0)

    def _fill_os_variant_model(self, model, _type):
        model.clear()

        preferred = self.config.preferred_distros
        variants = virtinst.OSDB.list_os(typename=_type, sortpref=preferred)
        supportl = virtinst.OSDB.list_os(typename=_type, sortpref=preferred,
            only_supported=True)

        for v in variants:
            supported = v in supportl or v.name == "generic"
            self._add_os_row(model, v.name, v.label, supported)

        self._add_os_row(model, sep=True)
        self._add_os_row(model, label=_("Show all OS options"), action=True)

    def populate_os_variant_model(self, _type):
        widget = self.widget("install-os-version")
        model = widget.get_model()
        self._fill_os_variant_model(model, _type)

        widget.set_active(0)

    def populate_media_model(self, model, urls):
        model.clear()
        if urls is not None:
            for url in urls:
                model.append([url])


    def change_caps(self, gtype=None, arch=None):
        if gtype is None:
            # If none specified, prefer HVM so install options aren't limited
            # with a default PV choice.
            for g in self.conn.caps.guests:
                if g.os_type == "hvm":
                    gtype = "hvm"
                    break

        capsinfo = self.conn.caps.guest_lookup(os_type=gtype, arch=arch)

        if self.capsinfo:
            if (self.capsinfo.guest == capsinfo.guest and
                self.capsinfo.domain == capsinfo.domain):
                return

        self.capsinfo = capsinfo
        logging.debug("Guest type set to os_type=%s, arch=%s, dom_type=%s",
                      self.capsinfo.os_type,
                      self.capsinfo.arch,
                      self.capsinfo.hypervisor_type)
        self.populate_machine()
        self.set_caps_state()

    def populate_summary(self):
        distro, version, ignore1, dlabel, vlabel = self.get_config_os_info()
        mem = self.pretty_memory(int(self.guest.memory))
        cpu = str(int(self.guest.vcpus))

        instmethod = self.get_config_install_page()
        install = ""
        if instmethod == INSTALL_PAGE_ISO:
            install = _("Local CDROM/ISO")
        elif instmethod == INSTALL_PAGE_URL:
            install = _("URL Install Tree")
        elif instmethod == INSTALL_PAGE_PXE:
            install = _("PXE Install")
        elif instmethod == INSTALL_PAGE_IMPORT:
            install = _("Import existing OS image")
        elif instmethod == INSTALL_PAGE_CONTAINER_APP:
            install = _("Application container")
        elif instmethod == INSTALL_PAGE_CONTAINER_OS:
            install = _("Operating system container")

        storagetmpl = "<span size='small' color='#484848'>%s</span>"
        disks = self.guest.get_devices("disk")
        if disks:
            disk = disks[0]
            storage = "%s" % self.pretty_storage(disk.get_size())

            # default storage is dependent on the VM name which the user
            # can change on the last page,  so this label can get out of date.
            # We could dynamically update it if user changes things, but
            # not sure if anyone cares.
            if not self.is_default_storage():
                storage += " " + (storagetmpl % disk.path)
        elif len(self.guest.get_devices("filesystem")):
            fs = self.guest.get_devices("filesystem")[0]
            storage = storagetmpl % fs.source
        elif self.guest.os.is_container():
            storage = _("Host filesystem")
        else:
            storage = _("None")

        osstr = ""
        have_os = True
        if self.guest.os.is_container():
            osstr = _("Linux")
        elif not distro:
            osstr = _("Generic")
            have_os = False
        elif not version:
            osstr = _("Generic") + " " + dlabel
            have_os = False
        else:
            osstr = vlabel

        self.widget("finish-warn-os").set_visible(not have_os)
        self.widget("summary-os").set_text(osstr)
        self.widget("summary-install").set_text(install)
        self.widget("summary-mem").set_text(mem)
        self.widget("summary-cpu").set_text(cpu)
        self.widget("summary-storage").set_markup(storage)

        self.netdev_changed(None)

    # get_* methods
    def get_config_name(self):
        return self.widget("create-vm-name").get_text()

    def get_config_machine(self):
        return uiutil.get_list_selection(self.widget("config-machine"),
            check_visible=True)

    def is_install_page(self):
        notebook = self.widget("create-pages")
        curpage = notebook.get_current_page()
        return curpage == PAGE_INSTALL

    def get_config_install_page(self):
        if self.widget("virt-install-box").get_visible():
            if self.widget("method-local").get_active():
                return INSTALL_PAGE_ISO
            elif self.widget("method-tree").get_active():
                return INSTALL_PAGE_URL
            elif self.widget("method-pxe").get_active():
                return INSTALL_PAGE_PXE
            elif self.widget("method-import").get_active():
                return INSTALL_PAGE_IMPORT
        else:
            if self.widget("method-container-app").get_active():
                return INSTALL_PAGE_CONTAINER_APP
            if self.widget("method-container-os").get_active():
                return INSTALL_PAGE_CONTAINER_OS

    def get_config_os_info(self):
        drow = uiutil.get_list_selected_row(self.widget("install-os-type"))
        vrow = uiutil.get_list_selected_row(self.widget("install-os-version"))
        distro = None
        dlabel = None
        variant = None
        variant_found = False
        vlabel = self.widget("install-os-version-entry").get_text()

        for i in self.widget("install-os-version").get_model():
            if not i[2] and not i[3] and i[1] == vlabel:
                variant = i[0]
                variant_found = True
                break

        if vlabel and not variant_found:
            return (None, None, False, None, None)

        if drow:
            distro = drow[0]
            dlabel = drow[1]
        if vrow:
            variant = vrow[0]
            vlabel = vrow[1]

        return (distro and str(distro),
                variant and str(variant),
                True,
                str(dlabel), str(vlabel))

    def get_config_local_media(self, store_media=False):
        if self.widget("install-local-cdrom").get_active():
            return self.mediacombo.get_path()
        else:
            ret = self.widget("install-local-box").get_child().get_text()
            if ret and store_media:
                self.config.add_iso_path(ret)
            return ret

    def get_config_detectable_media(self):
        instpage = self.get_config_install_page()
        media = ""

        if instpage == INSTALL_PAGE_ISO:
            media = self.get_config_local_media()
        elif instpage == INSTALL_PAGE_URL:
            media = self.widget("install-url-box").get_child().get_text()
        elif instpage == INSTALL_PAGE_IMPORT:
            media = self.widget("install-import-entry").get_text()

        return media

    def get_config_url_info(self, store_media=False):
        media = self.widget("install-url-box").get_child().get_text().strip()
        extra = self.widget("install-urlopts-entry").get_text().strip()
        ks = self.widget("install-ks-box").get_child().get_text().strip()

        if media and store_media:
            self.config.add_media_url(media)
        if ks and store_media:
            self.config.add_kickstart_url(ks)

        return (media.strip(), extra.strip(), ks.strip())

    def get_config_import_path(self):
        return self.widget("install-import-entry").get_text()

    def get_config_container_app_path(self):
        return self.widget("install-app-entry").get_text()

    def get_config_container_fs_path(self):
        return self.widget("install-oscontainer-fs").get_text()

    def is_default_storage(self):
        return (self.addstorage.is_default_storage() and
                not self.skip_disk_page())

    def get_config_customize(self):
        return self.widget("summary-customize").get_active()
    def is_detect_active(self):
        return self.widget("install-detect-os").get_active()


    ################
    # UI Listeners #
    ################

    def _close_requested(self, *ignore1, **ignore2):
        if (self.failed_guest and
            self.failed_guest.get_created_disks()):

            def _cleanup_disks(asyncjob):
                meter = asyncjob.get_meter()
                self.failed_guest.cleanup_created_disks(meter)

            def _cleanup_disks_finished(error, details):
                if error:
                    logging.debug("Error cleaning up disk images:"
                        "\nerror=%s\ndetails=%s", error, details)
                self.idle_add(self._close)

            progWin = vmmAsyncJob(
                _cleanup_disks, [],
                _cleanup_disks_finished, [],
                _("Removing disk images"),
                _("Removing disk images we created for this virtual machine."),
                self.topwin)
            progWin.run()

        else:
            self._close()

        return 1

    def conn_changed(self, src):
        uri = uiutil.get_list_selection(src)
        conn = None
        if uri:
            conn = self.engine.conns[uri]["conn"]

        # If we aren't visible, let reset_state handle this for us, which
        # has a better chance of reporting error
        if not self.is_visible():
            return

        self.set_conn(conn)

    def method_changed(self, src):
        ignore = src
        self.set_page_num_text(0)

    def machine_changed(self, ignore):
        machine = self.get_config_machine()
        show_dtb_virtio = (self.capsinfo.arch == "armv7l" and
                           machine in ["vexpress-a9", "vexpress-15"])
        uiutil.set_grid_row_visible(
            self.widget("config-dtb-warn-virtio"), show_dtb_virtio)

    def netdev_changed(self, ignore):
        row = self.netlist.get_network_row()
        show_pxe_warn = True
        pxe_install = (self.get_config_install_page() == INSTALL_PAGE_PXE)
        expand = False

        if row:
            ntype = row[0]
            connkey = row[6]

            expand = (ntype != "network" and ntype != "bridge")
            if (ntype is None or
                ntype == virtinst.VirtualNetworkInterface.TYPE_USER):
                show_pxe_warn = True
            elif ntype != virtinst.VirtualNetworkInterface.TYPE_VIRTUAL:
                show_pxe_warn = False
            else:
                obj = self.conn.get_net(connkey)
                show_pxe_warn = not obj.can_pxe()

        show_warn = (show_pxe_warn and pxe_install)

        if expand or show_warn:
            self.widget("config-advanced-expander").set_expanded(True)
        self.widget("config-netdev-warn-box").set_visible(show_warn)
        self.widget("config-netdev-warn-label").set_markup(
            "<small>%s</small>" % _("Network selection does not support PXE"))

    def hv_changed(self, src):
        hv = uiutil.get_list_selection(src, column=1)
        if not hv:
            return

        self.change_caps(hv)
        self.populate_arch()

    def arch_changed(self, src):
        arch = uiutil.get_list_selection(src)
        if not arch:
            return

        self.change_caps(self.capsinfo.os_type, arch)

    def media_box_changed(self, widget):
        self.mediaDetected = False

        # If the widget has focus, don't fire detect_media_os, it means
        # the user is probably typing
        if self.widget(widget).get_child().has_focus():
            return

        self.detect_media_os()

    def url_box_changed(self, ignore):
        self.media_box_changed("install-url-box")

    def local_box_changed(self, ignore):
        self.media_box_changed("install-local-box")

    def should_detect_media(self):
        return (self.is_detect_active() and not self.mediaDetected)

    def detect_media_os(self, ignore1=None, forward=False):
        if not self.should_detect_media():
            return
        if not self.is_install_page():
            return
        self.start_detection(forward=forward)

    def toggle_detect_os(self, src):
        dodetect = src.get_active()

        self.widget("install-os-type-label").set_visible(dodetect)
        self.widget("install-os-version-label").set_visible(dodetect)
        self.widget("install-os-type").set_visible(not dodetect)
        self.widget("install-os-version").set_visible(not dodetect)

        if dodetect:
            self.widget("install-os-version-entry").set_text("")
            self.mediaDetected = False
            self.detect_media_os()

    def _selected_os_row(self):
        return uiutil.get_list_selected_row(self.widget("install-os-type"))

    def change_os_type(self, box):
        ignore = box
        row = self._selected_os_row()
        if row:
            _type = row[0]
            self.populate_os_variant_model(_type)
            if row[3]:
                self.show_all_os = True
                self.populate_os_type_model()
                return

    def change_os_version(self, box):
        show_all = uiutil.get_list_selection(box, column=3, check_entry=False)
        if not show_all:
            return

        # 'show all OS' was clicked
        # Get previous type to reselect it later
        type_row = self._selected_os_row()
        if not type_row:
            return
        old_type = type_row[0]

        self.show_all_os = True
        self.populate_os_type_model()

        # Reselect previous type row
        os_type_list = self.widget("install-os-type")
        os_type_model = os_type_list.get_model()
        for idx in range(len(os_type_model)):
            if os_type_model[idx][0] == old_type:
                os_type_list.set_active(idx)
                break

    def toggle_local_cdrom(self, src):
        is_active = src.get_active()
        if is_active and self.mediacombo.get_path():
            # Local CDROM was selected with media preset, detect distro
            self.mediaDetected = False
            self.detect_media_os()

        self.widget("install-local-cdrom-align").set_sensitive(is_active)

    def toggle_local_iso(self, src):
        uselocal = src.get_active()
        self.widget("install-local-box").set_sensitive(uselocal)
        self.widget("install-local-browse").set_sensitive(uselocal)
        self.mediaDetected = False
        self.detect_media_os()

    def detect_visibility_changed(self, src, ignore=None):
        is_visible = src.get_visible()
        detect_chkbox = self.widget("install-detect-os")
        nodetect_label = self.widget("install-nodetect-label")

        detect_chkbox.set_active(is_visible)
        detect_chkbox.toggled()

        if is_visible:
            nodetect_label.hide()
        else:
            nodetect_label.show()

    def browse_oscontainer(self, ignore):
        self._browse_file("install-oscontainer-fs", is_dir=True)
    def browse_app(self, ignore):
        self._browse_file("install-app-entry")
    def browse_import(self, ignore):
        self._browse_file("install-import-entry")
    def browse_iso(self, ignore):
        def set_path(ignore, path):
            self.widget("install-local-box").get_child().set_text(path)
        self._browse_file(None, cb=set_path, is_media=True)
        self.widget("install-local-box").activate()
    def browse_kernel(self, ignore):
        self._browse_file("config-kernel")
    def browse_initrd(self, ignore):
        self._browse_file("config-initrd")
    def browse_dtb(self, ignore):
        self._browse_file("config-dtb")

    def toggle_enable_storage(self, src):
        self.widget("config-storage-align").set_sensitive(src.get_active())

    # Navigation methods
    def set_install_page(self):
        instnotebook = self.widget("install-method-pages")
        detectbox = self.widget("install-detect-os-box")
        osbox = self.widget("install-os-distro-box")
        instpage = self.get_config_install_page()

        # Setting OS value for a container guest doesn't really matter
        # at the moment
        iscontainer = instpage in [INSTALL_PAGE_CONTAINER_APP,
                                   INSTALL_PAGE_CONTAINER_OS]
        osbox.set_visible(iscontainer)

        enabledetect = (instpage == INSTALL_PAGE_ISO and
                        self.conn and
                        not self.conn.is_remote() or
                        self.get_config_install_page() == INSTALL_PAGE_URL)

        detectbox.set_visible(enabledetect)

        if instpage == INSTALL_PAGE_PXE:
            # Hide the install notebook for pxe, since there isn't anything
            # to ask for
            instnotebook.hide()
        else:
            instnotebook.show()


        instnotebook.set_current_page(instpage)

    def container_install(self):
        return self.get_config_install_page() in [INSTALL_PAGE_CONTAINER_APP,
                                                  INSTALL_PAGE_CONTAINER_OS]
    def skip_disk_page(self):
        return self.get_config_install_page() in [INSTALL_PAGE_IMPORT,
                                                  INSTALL_PAGE_CONTAINER_APP,
                                                  INSTALL_PAGE_CONTAINER_OS]

    def back(self, src_ignore):
        notebook = self.widget("create-pages")
        curpage = notebook.get_current_page()
        next_page = curpage - 1

        if curpage == PAGE_FINISH and self.skip_disk_page():
            # Skip over storage page
            next_page -= 1

        notebook.set_current_page(next_page)

    def _get_next_pagenum(self, curpage):
        next_page = curpage + 1

        if next_page == PAGE_STORAGE and self.skip_disk_page():
            # Skip storage page for import installs
            next_page += 1

        return next_page

    def forward(self, src_ignore=None):
        notebook = self.widget("create-pages")
        curpage = notebook.get_current_page()

        if self.have_startup_error:
            return

        if (curpage == PAGE_INSTALL and self.should_detect_media() and
            self.get_config_detectable_media()):
            # Make sure we have detected the OS before validating the page
            self.detect_media_os(forward=True)
            return

        if self.validate(curpage) is not True:
            return

        if curpage == PAGE_NAME:
            self.set_install_page()

        next_page = self._get_next_pagenum(curpage)

        self.widget("create-forward").grab_focus()
        notebook.set_current_page(next_page)

    def set_page_num_text(self, cur):
        cur += 1
        final = PAGE_FINISH + 1
        if self.skip_disk_page():
            final -= 1
            cur = min(cur, final)

        page_lbl = ("<span color='#59B0E2'>%s</span>" %
                    _("Step %(current_page)d of %(max_page)d") %
                    {'current_page': cur, 'max_page': final})

        self.widget("header-pagenum").set_markup(page_lbl)

    def page_changed(self, ignore1, ignore2, pagenum):
        if pagenum == PAGE_INSTALL:
            self.detect_media_os()
            self.widget("install-os-distro-box").set_visible(
                not self.container_install())
        elif pagenum == PAGE_FINISH:
            try:
                self.populate_summary()
            except Exception, e:
                self.err.show_err(_("Error populating summary page: %s") %
                    str(e))
                return

            self.widget("create-finish").grab_focus()

        self.set_page_num_text(pagenum)
        self.widget("create-back").set_sensitive(pagenum != PAGE_NAME)
        self.widget("create-forward").set_visible(pagenum != PAGE_FINISH)
        self.widget("create-finish").set_visible(pagenum == PAGE_FINISH)

        for nr in range(self.widget("create-pages").get_n_pages()):
            page = self.widget("create-pages").get_nth_page(nr)
            page.set_visible(nr == pagenum)

    def build_guest(self, variant):
        guest = self.conn.caps.build_virtinst_guest(self.capsinfo)
        guest.os.machine = self.get_config_machine()

        # Generate UUID (makes customize dialog happy)
        try:
            guest.uuid = util.randomUUID(guest.conn)
        except Exception, e:
            self.err.show_err(_("Error setting UUID: %s") % str(e))
            return None

        # OS distro/variant validation
        try:
            if variant:
                guest.os_variant = variant
        except ValueError, e:
            self.err.val_err(_("Error setting OS information."), str(e))
            return None

        if guest.os.is_arm64():
            try:
                guest.set_uefi_default()
            except:
                # If this errors we will have already informed the user
                # on page 1.
                pass

        # Set up default devices
        try:
            guest.default_graphics_type = self.config.get_graphics_type()
            guest.skip_default_sound = not self.config.get_new_vm_sound()
            guest.skip_default_usbredir = (
                self.config.get_add_spice_usbredir() == "no")
            guest.x86_cpu_default = self.config.get_default_cpu_setting(
                for_cpu=True)

            guest.add_default_devices()
        except Exception, e:
            self.err.show_err(_("Error setting up default devices:") + str(e))
            return None

        return guest

    def validate(self, pagenum):
        try:
            if pagenum == PAGE_NAME:
                return self.validate_intro_page()
            elif pagenum == PAGE_INSTALL:
                return self.validate_install_page()
            elif pagenum == PAGE_MEM:
                return self.validate_mem_page()
            elif pagenum == PAGE_STORAGE:
                return self.validate_storage_page()
            elif pagenum == PAGE_FINISH:
                return self.validate_final_page()
        except Exception, e:
            self.err.show_err(_("Uncaught error validating install "
                                "parameters: %s") % str(e))
            return

    def validate_intro_page(self):
        # We just set this here because it's needed soon after for distro
        # detection. But the 'real' self.guest is created in validate_install,
        # and it just uses build_guest, so don't ever add any other guest
        # altering here.
        self.guest = self.build_guest(None)
        if not self.guest:
            return False
        return True

    def _generate_default_name(self, distro, variant):
        force_num = False
        if self.guest.os.is_container():
            basename = "container"
            force_num = True
        elif not distro:
            basename = "vm"
            force_num = True
        elif not variant:
            basename = distro
        else:
            basename = variant

        if self.guest.os.arch != self.conn.caps.host.cpu.arch:
            basename += "-%s" % pretty_arch(self.guest.os.arch)
            force_num = False

        return util.generate_name(basename,
            self.conn.get_backend().lookupByName,
            start_num=force_num and 1 or 2, force_num=force_num,
            sep=not force_num and "-" or "",
            collidelist=[vm.get_name() for vm in self.conn.list_vms()])

    def validate_install_page(self):
        instmethod = self.get_config_install_page()
        installer = None
        location = None
        extra = None
        ks = None
        cdrom = False
        is_import = False
        init = None
        fs = None
        distro, variant, valid, ignore1, ignore2 = self.get_config_os_info()

        if not valid:
            return self.err.val_err(_("Please specify a valid OS variant."))

        if instmethod == INSTALL_PAGE_ISO:
            instclass = virtinst.DistroInstaller
            media = self.get_config_local_media()

            if not media:
                return self.err.val_err(
                                _("An install media selection is required."))

            location = media
            cdrom = True

        elif instmethod == INSTALL_PAGE_URL:
            instclass = virtinst.DistroInstaller
            media, extra, ks = self.get_config_url_info()

            if not media:
                return self.err.val_err(_("An install tree is required."))

            location = media

        elif instmethod == INSTALL_PAGE_PXE:
            instclass = virtinst.PXEInstaller

        elif instmethod == INSTALL_PAGE_IMPORT:
            instclass = virtinst.ImportInstaller
            is_import = True

            import_path = self.get_config_import_path()
            if not import_path:
                return self.err.val_err(
                                _("A storage path to import is required."))

        elif instmethod == INSTALL_PAGE_CONTAINER_APP:
            instclass = virtinst.ContainerInstaller

            init = self.get_config_container_app_path()
            if not init:
                return self.err.val_err(_("An application path is required."))

        elif instmethod == INSTALL_PAGE_CONTAINER_OS:
            instclass = virtinst.ContainerInstaller

            fs = self.get_config_container_fs_path()
            if not fs:
                return self.err.val_err(_("An OS directory path is required."))

        # Build the installer and Guest instance
        try:
            # Overwrite the guest
            installer = instclass(self.conn.get_backend())
            self.guest = self.build_guest(variant or distro)
            if not self.guest:
                return False
            self.guest.installer = installer
        except Exception, e:
            return self.err.val_err(
                        _("Error setting installer parameters."), e)

        # Validate media location
        try:
            if location is not None:
                self.guest.installer.location = location
            if cdrom:
                self.guest.installer.cdrom = True

            extraargs = ""
            if extra:
                extraargs += extra
            if ks:
                extraargs += " ks=%s" % ks

            if extraargs:
                self.guest.installer.extraargs = extraargs

            if init:
                self.guest.os.init = init

            if fs:
                fsdev = virtinst.VirtualFilesystem(self.guest.conn)
                fsdev.target = "/"
                fsdev.source = fs
                self.guest.add_device(fsdev)
        except Exception, e:
            return self.err.val_err(
                                _("Error setting install media location."), e)

        # Setting kernel
        if instmethod == INSTALL_PAGE_IMPORT:
            kernel = self.widget("config-kernel").get_text() or None
            kargs = self.widget("config-kernel-args").get_text() or None
            initrd = self.widget("config-initrd").get_text() or None
            dtb = self.widget("config-dtb").get_text() or None

            if not self.widget("config-dtb").get_visible():
                dtb = None
            if not self.widget("config-kernel").get_visible():
                kernel = None
                initrd = None
                kargs = None

            self.guest.os.kernel = kernel
            self.guest.os.initrd = initrd
            self.guest.os.dtb = dtb
            self.guest.os.kernel_args = kargs

            require_kernel = ("arm" in self.capsinfo.arch)
            if require_kernel and not kernel:
                return self.err.val_err(
                    _("A kernel is required for %s guests.") %
                    self.capsinfo.arch)

        try:
            name = self._generate_default_name(distro, variant)
            self.widget("create-vm-name").set_text(name)
            self.guest.name = name
        except Exception, e:
            return self.err.val_err(_("Error setting default name."), e)

        # Kind of wonky, run storage validation now, which will assign
        # the import path. Import installer skips the storage page.
        if is_import:
            if not self.validate_storage_page():
                return False

        if self.guest.installer.scratchdir_required():
            path = util.make_scratchdir(self.guest.conn, self.guest.type)
        elif instmethod == INSTALL_PAGE_ISO:
            path = self.guest.installer.location
        else:
            path = None

        if path:
            self.addstorage.check_path_search(
                self, self.conn, path)

        res = None
        osobj = virtinst.OSDB.lookup_os(variant)
        if osobj:
            res = osobj.get_recommended_resources(self.guest)
            logging.debug("Recommended resources for variant=%s: %s",
                variant, res)

        # Change the default values suggested to the user.
        ram_size = DEFAULT_MEM
        if res and res.get("ram") > 0:
            ram_size = res["ram"] / (1024 ** 2)
        self.widget("config-mem").set_value(ram_size)

        n_cpus = 1
        if res and res.get("n-cpus") > 0:
            n_cpus = res["n-cpus"]
        self.widget("config-cpus").set_value(n_cpus)

        storage_size = 20
        if res and res.get("storage"):
            storage_size = int(res["storage"]) / (1024 ** 3)
        self.addstorage.widget("config-storage-size").set_value(storage_size)

        # Validation passed, store the install path (if there is one) in
        # gsettings
        self.get_config_local_media(store_media=True)
        self.get_config_url_info(store_media=True)
        return True

    def validate_mem_page(self):
        cpus = self.widget("config-cpus").get_value()
        mem  = self.widget("config-mem").get_value()

        # VCPUS
        try:
            self.guest.vcpus = int(cpus)
        except Exception, e:
            return self.err.val_err(_("Error setting CPUs."), e)

        # Memory
        try:
            self.guest.memory = int(mem) * 1024
            self.guest.maxmemory = int(mem) * 1024
        except Exception, e:
            return self.err.val_err(_("Error setting guest memory."), e)

        return True

    def validate_storage_page(self):
        failed_disk = None
        if self.disk and self.disk in self.guest.get_devices("disk"):
            self.guest.remove_device(self.disk)
            if self.failed_guest:
                failed_disk = self.disk
        self.disk = None

        path = None
        path_already_created = False

        if self.get_config_install_page() == INSTALL_PAGE_IMPORT:
            path = self.get_config_import_path()

        elif self.is_default_storage():
            # Don't generate a new path if the install failed
            if failed_disk and failed_disk.device == "disk":
                path = failed_disk.path
                path_already_created = failed_disk.storage_was_created
                logging.debug("Reusing failed disk path=%s "
                    "already_created=%s", path, path_already_created)
            else:
                path = self.addstorage.get_default_path(self.guest.name)
                logging.debug("Default storage path is: %s", path)

        ret = self.addstorage.validate_storage(self.guest.name, path=path)
        no_storage = (ret is True)

        if self.get_config_install_page() == INSTALL_PAGE_ISO:
            # CD/ISO install and no disks implies LiveCD
            self.guest.installer.livecd = no_storage

        if ret in [True, False]:
            return ret

        if self.addstorage.validate_disk_object(ret) is False:
            return False

        self.disk = ret
        self.disk.storage_was_created = path_already_created
        self.guest.add_device(self.disk)

        return True

    def validate_final_page(self):
        # HV + Arch selection
        name = self.get_config_name()
        if name != self.guest.name:
            try:
                self.guest.name = name
            except Exception, e:
                return self.err.val_err(_("Invalid guest name"), str(e))
            if self.is_default_storage():
                logging.debug("User changed VM name and using default "
                    "storage, re-validating with new default storage path.")
                # User changed the name and we are using default storage
                # which depends on the VM name. Revalidate things
                if not self.validate_storage_page():
                    return False

        nettype = self.netlist.get_network_selection()[0]
        if nettype is None:
            # No network device available
            instmethod = self.get_config_install_page()
            methname = None
            if instmethod == INSTALL_PAGE_PXE:
                methname  = "PXE"
            elif instmethod == INSTALL_PAGE_URL:
                methname = "URL"

            if methname:
                return self.err.val_err(
                            _("Network device required for %s install.") %
                            methname)

        macaddr = virtinst.VirtualNetworkInterface.generate_mac(
            self.conn.get_backend())
        nic = self.netlist.validate_network(macaddr)
        if nic is False:
            return False

        if self.nic and self.nic in self.guest.get_devices("interface"):
            self.guest.remove_device(self.nic)
        if nic:
            self.nic = nic
            self.guest.add_device(self.nic)

        return True

    def _undo_finish_cursor(self):
        self.topwin.set_sensitive(True)
        self.topwin.get_window().set_cursor(
            Gdk.Cursor.new(Gdk.CursorType.TOP_LEFT_ARROW))

    def finish(self, src_ignore):
        # Validate the final page
        page = self.widget("create-pages").get_current_page()
        if self.validate(page) is not True:
            return False

        logging.debug("Starting create finish() sequence")
        guest = self.guest

        # Start the install
        self.failed_guest = None
        self.topwin.set_sensitive(False)
        self.topwin.get_window().set_cursor(
            Gdk.Cursor.new(Gdk.CursorType.WATCH))

        if self.get_config_customize():
            logging.debug("User requested 'customize', launching dialog")
            try:
                self.customize(guest)
            except Exception, e:
                self._undo_finish_cursor()
                self.err.show_err(_("Error starting installation: ") + str(e))
                return
        else:
            self.start_install(guest)

    def customize(self, guest):
        virtinst_guest = vmmDomainVirtinst(self.conn, guest, self.guest.uuid)

        def cleanup_config_window():
            if self.config_window:
                for s in self.config_window_signals:
                    self.config_window.disconnect(s)
                self.config_window.cleanup()
                self.config_window = None

        def start_install_wrapper(ignore, guest):
            cleanup_config_window()
            if not self.is_visible():
                return
            logging.debug("User finished customize dialog, starting install")
            guest.check_defaults()
            self.start_install(guest)

        def config_canceled(ignore):
            logging.debug("User closed customize window, closing wizard")
            cleanup_config_window()
            self._undo_finish_cursor()
            self._close_requested()

        cleanup_config_window()
        self.config_window = vmmDetails(virtinst_guest, self.topwin)
        self.config_window_signals = []
        self.config_window_signals.append(
            self.config_window.connect("customize-finished",
                                       start_install_wrapper,
                                       guest))
        self.config_window_signals.append(
            self.config_window.connect("details-closed", config_canceled))
        self.config_window.show()

    def _install_finished_cb(self, error, details):
        self._undo_finish_cursor()

        if error:
            error = (_("Unable to complete install: '%s'") % error)
            self.err.show_err(error,
                              details=details)
            self.failed_guest = self.guest
            return

        self._close()

        # Launch details dialog for new VM
        self.emit("action-show-domain", self.conn.get_uri(), self.guest.name)


    def start_install(self, guest):
        progWin = vmmAsyncJob(self.do_install, [guest],
                              self._install_finished_cb, [],
                              _("Creating Virtual Machine"),
                              _("The virtual machine is now being "
                                "created. Allocation of disk storage "
                                "and retrieval of the installation "
                                "images may take a few minutes to "
                                "complete."),
                              self.topwin)
        progWin.run()

    def do_install(self, asyncjob, guest):
        meter = asyncjob.get_meter()

        # Build a list of pools we should refresh, if we are creating storage
        refresh_pools = []
        for disk in guest.get_devices("disk"):
            if not disk.wants_storage_creation():
                continue

            pool = disk.get_parent_pool()
            if not pool:
                continue

            poolname = pool.name()
            if poolname not in refresh_pools:
                refresh_pools.append(poolname)

        logging.debug("Starting background install process")
        guest.start_install(meter=meter)
        logging.debug("Install completed")

        # Wait for VM to show up
        self.conn.schedule_priority_tick(pollvm=True)
        count = 0
        foundvm = None
        while count < 100:
            for vm in self.conn.list_vms():
                if vm.get_uuid() == guest.uuid:
                    foundvm = vm
            if foundvm:
                break
            count += 1
            time.sleep(.1)

        if not foundvm:
            raise RuntimeError(
                _("VM '%s' didn't show up after expected time.") % guest.name)
        vm = foundvm

        if vm.is_shutoff():
            # Domain is already shutdown, but no error was raised.
            # Probably means guest had no 'install' phase, as in
            # for live cds. Try to restart the domain.
            vm.startup()
        elif guest.installer.has_install_phase():
            # Register a status listener, which will restart the
            # guest after the install has finished
            def cb():
                vm.connect_opt_out("state-changed",
                                   self.check_install_status, guest)
                return False
            self.idle_add(cb)

        # Kick off pool updates
        for poolname in refresh_pools:
            try:
                pool = self.conn.get_pool(poolname)
                self.idle_add(pool.refresh)
            except:
                logging.debug("Error looking up pool=%s for refresh after "
                    "VM creation.", poolname, exc_info=True)


    def check_install_status(self, vm, virtinst_guest):
        if vm.is_crashed():
            logging.debug("VM crashed, cancelling install plans.")
            return True

        if not vm.is_shutoff():
            return

        try:
            if virtinst_guest:
                continue_inst = virtinst_guest.get_continue_inst()

                if continue_inst:
                    logging.debug("VM needs a 2 stage install, continuing.")
                    # Continue the install, then reconnect this opt
                    # out handler, removing the virtinst_guest which
                    # will force one final restart.
                    virtinst_guest.continue_install()

                    vm.connect_opt_out("state-changed",
                                       self.check_install_status, None)
                    return True

            if vm.get_install_abort():
                logging.debug("User manually shutdown VM, not restarting "
                              "guest after install.")
                return True

            logging.debug("Install should be completed, starting VM.")
            vm.startup()
        except Exception, e:
            self.err.show_err(_("Error continue install: %s") % str(e))

        return True

    def pretty_storage(self, size):
        return "%.1f GiB" % float(size)

    def pretty_memory(self, mem):
        return "%d MiB" % (mem / 1024.0)


    # Distro detection methods
    def set_distro_labels(self, distro, ver, force=False):
        # Helper to set auto detect result labels
        if not force and not self.is_detect_active():
            return

        self.widget("install-os-type-label").set_text(distro)
        self.widget("install-os-version-label").set_text(ver)

    def set_os_val(self, os_widget, value):
        # Helper method to set the OS Type/Variant selections to the passed
        # values, or -1 if not present.
        model = os_widget.get_model()
        def find_row_value():
            for idx in range(len(model)):
                row = model[idx]
                if value and row[0] == value:
                    os_widget.set_active(idx)
                    return row[1]
            os_widget.set_active(0)

        ret = None
        if value:
            ret = find_row_value()

            if not ret and not self.show_all_os:
                # We didn't find the OS in the variant UI, but we are only
                # showing the reduced OS list. Trigger the show_all_os option,
                # and try again.
                os_widget.set_active(len(model) - 1)
                ret = find_row_value()
        return ret or _("Unknown")

    def set_distro_selection(self, variant):
        # Wrapper to change OS Type/Variant values, and update the distro
        # detection labels
        if not self.is_detect_active():
            return

        distro_type = None
        distro_var = None
        if variant:
            osclass = virtinst.OSDB.lookup_os(variant)
            distro_type = osclass.get_typename()
            distro_var = osclass.name

        dl = self.set_os_val(self.widget("install-os-type"), distro_type)
        vl = self.set_os_val(self.widget("install-os-version"), distro_var)
        self.set_distro_labels(dl, vl)

    def check_detection(self, idx, forward):
        results = None
        try:
            base = _("Detecting")

            if (self.detectedDistro == DETECT_INPROGRESS and
                (idx < (DETECT_TIMEOUT * 2))):
                detect_str = base + ("." * ((idx % 3) + 1))
                self.set_distro_labels(detect_str, detect_str)

                self.timeout_add(500, self.check_detection,
                                      idx + 1, forward)
                return

            results = self.detectedDistro
        except:
            logging.exception("Error in distro detect timeout")

        if results in [DETECT_INPROGRESS, DETECT_FAILED]:
            results = None

        self.widget("create-forward").set_sensitive(True)
        self.mediaDetected = True
        logging.debug("Finished OS detection.")
        self.set_distro_selection(results)
        if forward:
            self.idle_add(self.forward, ())

    def start_detection(self, forward):
        if self.detectedDistro == DETECT_INPROGRESS:
            return

        media = self.get_config_detectable_media()
        if not media:
            return

        self.detectedDistro = DETECT_INPROGRESS

        logging.debug("Starting OS detection thread for media=%s", media)
        self.widget("create-forward").set_sensitive(False)

        detectThread = threading.Thread(target=self.actually_detect,
                                        name="Actual media detection",
                                        args=(media,))
        detectThread.setDaemon(True)
        detectThread.start()

        self.check_detection(0, forward)

    def actually_detect(self, media):
        try:
            installer = virtinst.DistroInstaller(self.conn.get_backend())
            installer.location = media

            self.detectedDistro = installer.detect_distro(self.guest)
        except:
            logging.exception("Error detecting distro.")
            self.detectedDistro = DETECT_FAILED

    def _browse_file_cb(self, ignore, widget):
        self._browse_file(widget)

    def _stable_defaults(self):
        emu = None
        if self.guest:
            emu = self.guest.emulator
        elif self.capsinfo:
            emu = self.capsinfo.emulator

        ret = self.conn.stable_defaults(emu)
        return ret

    def _browse_file(self, cbwidget, cb=None, is_media=False, is_dir=False):
        if is_media:
            reason = self.config.CONFIG_DIR_ISO_MEDIA
        elif is_dir:
            reason = self.config.CONFIG_DIR_FS
        else:
            reason = self.config.CONFIG_DIR_IMAGE

        if cb:
            callback = cb
        else:
            def callback(ignore, text):
                widget = cbwidget
                if type(cbwidget) is str:
                    widget = self.widget(cbwidget)
                widget.set_text(text)

        if self.storage_browser and self.storage_browser.conn != self.conn:
            self.storage_browser.cleanup()
            self.storage_browser = None
        if self.storage_browser is None:
            self.storage_browser = vmmStorageBrowser(self.conn)

        self.storage_browser.set_stable_defaults(self._stable_defaults())
        self.storage_browser.set_vm_name(self.get_config_name())
        self.storage_browser.set_finish_cb(callback)
        self.storage_browser.set_browse_reason(reason)
        self.storage_browser.show(self.topwin)
