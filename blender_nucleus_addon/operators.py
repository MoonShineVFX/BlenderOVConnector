# SPDX-License-Identifier: MIT
# Phase 4 operators: connect, disconnect, browse, refresh, add bookmark,
# parent dir, and the auth-prompt modal popup.
# Phase 5 adds: ImportUSD (open from Nucleus) and ExportUSD (save to Nucleus).

import base64
import shutil
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator

from .preferences import add_bookmark, get_bookmarks, remove_bookmark
from . import event_handlers
from .rpc_client import get_client, RpcError
from .browser import init_connection_list, init_location_list, populate_file_list
from . import usd_deps


def _settings(context):
    return context.scene.omni_nucleus


# Data-block collections we wipe when switching main USD files. Listed by
# name so a future Blender version that drops one of them won't crash us.
_BPY_DATA_COLLECTIONS = (
    "meshes", "materials", "images", "textures", "node_groups",
    "curves", "armatures", "lights", "cameras", "actions",
    "lattices", "metaballs", "grease_pencils", "fonts", "speakers",
    "volumes", "movieclips", "sounds", "linestyles", "particles",
)


def _clear_loaded_stage(context):
    """Wipe scene contents so a new main USD can be loaded into a clean
    slate. Mirrors the Omniverse "one stage at a time" model — opening a
    new file replaces, not augments. Keeps the active scene and the addon
    settings (bookmarks, connections, file list, etc.) intact.
    """
    for obj in list(bpy.data.objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    for coll in list(bpy.data.collections):
        try:
            bpy.data.collections.remove(coll)
        except Exception:
            pass
    for name in _BPY_DATA_COLLECTIONS:
        dc = getattr(bpy.data, name, None)
        if dc is None:
            continue
        for item in list(dc):
            if getattr(item, "users", 0) == 0:
                try:
                    dc.remove(item)
                except Exception:
                    pass

    s = _settings(context)
    s.source_url = ""
    s.temp_filepath = ""


# --- connection management -------------------------------------------------

class OMNI_OT_OpenConnection(Operator):
    """Connect to a Nucleus server (URL is the value in the Directory field
    or the selected location). Triggers auth flow if not already signed in."""
    bl_idname = "omni.open_connection"
    bl_label = "Open Connection"

    url: StringProperty(name="Server URL", default="omniverse://")

    def invoke(self, context, event):
        # Pre-fill from selected location or directory.
        settings = _settings(context)
        if settings.location_list_index >= 0 and settings.location_list_index < len(settings.location_list):
            self.url = settings.location_list[settings.location_list_index].name
        elif settings.directory:
            self.url = settings.directory
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Server URL (e.g. omniverse://nucleus.example.com)")
        col.prop(self, "url", text="")

    def execute(self, context):
        url = self.url.strip().rstrip("/")
        if not url.startswith("omniverse://"):
            self.report({"ERROR"}, "URL must start with omniverse://")
            return {"CANCELLED"}

        client = get_client()
        try:
            client.call_blocking("initialize")
            # A list against the server root forces a connect + auth flow.
            client.call_blocking("list", url=url + "/", timeout=30.0)
        except (RpcError, TimeoutError) as exc:
            self.report({"ERROR"}, f"Connect failed: {exc}")
            return {"CANCELLED"}

        _settings(context).directory = url + "/"
        self.report({"INFO"}, f"Connected to {url}")
        return {"FINISHED"}


class OMNI_OT_CloseConnection(Operator):
    """Sign out of the currently selected Nucleus server."""
    bl_idname = "omni.close_connection"
    bl_label = "Close Connection"

    @classmethod
    def poll(cls, context):
        settings = _settings(context)
        return (settings.connection_list_index >= 0
                and settings.connection_list_index < len(settings.connection_list))

    def execute(self, context):
        settings = _settings(context)
        host = settings.connection_list[settings.connection_list_index].name
        url = f"omniverse://{host}"
        client = get_client()
        try:
            client.call_blocking("sign_out", url=url, timeout=10.0)
        except (RpcError, TimeoutError) as exc:
            self.report({"ERROR"}, f"sign_out failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Signed out of {host}")
        return {"FINISHED"}


# --- file browser ----------------------------------------------------------

class OMNI_OT_RefreshDirectory(Operator):
    """Re-list the current directory."""
    bl_idname = "omni.refresh_directory"
    bl_label = "Refresh"

    def execute(self, context):
        settings = _settings(context)
        if not settings.directory:
            self.report({"WARNING"}, "No directory selected")
            return {"CANCELLED"}
        populate_file_list(settings.directory, context)
        return {"FINISHED"}


class OMNI_OT_OpenParentDirectory(Operator):
    """Move up one folder."""
    bl_idname = "omni.open_parent_directory"
    bl_label = "Up"

    @classmethod
    def poll(cls, context):
        return bool(_settings(context).directory)

    def execute(self, context):
        d = _settings(context).directory.rstrip("/")
        # Compute parent without omni.client: omniverse://host/a/b/ -> omniverse://host/a/
        if d.startswith("omniverse://"):
            scheme_host, _, path = d.partition("/")  # omniverse: + // + host/path
        # Simpler: just slice up to the previous /.
        parts = d.split("/")
        if len(parts) <= 3:
            # Already at server root (omniverse://host).
            return {"CANCELLED"}
        parent = "/".join(parts[:-1]) + "/"
        _settings(context).directory = parent
        return {"FINISHED"}


class OMNI_OT_CreateDirectory(Operator):
    """Create a new subfolder in the current directory."""
    bl_idname = "omni.create_directory"
    bl_label = "New Folder"

    folder_name: StringProperty(name="Folder Name", default="new_folder")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        settings = _settings(context)
        if not settings.directory:
            self.report({"ERROR"}, "Set a directory first")
            return {"CANCELLED"}
        new_url = settings.directory.rstrip("/") + "/" + self.folder_name.strip().strip("/")
        client = get_client()
        try:
            client.call_blocking("initialize")
            client.call_blocking("create_folder", url=new_url + "/", timeout=15.0)
        except (RpcError, TimeoutError) as exc:
            self.report({"ERROR"}, f"create_folder failed: {exc}")
            return {"CANCELLED"}
        populate_file_list(settings.directory, context)
        return {"FINISHED"}


class OMNI_OT_AddBookmark(Operator):
    """Bookmark the current directory (or its server root)."""
    bl_idname = "omni.add_bookmark"
    bl_label = "Bookmark Current"

    def execute(self, context):
        d = _settings(context).directory.rstrip("/")
        if not d:
            self.report({"WARNING"}, "No directory to bookmark")
            return {"CANCELLED"}
        if not d.startswith("omniverse://"):
            self.report({"ERROR"}, "Bookmarks must be omniverse:// URLs")
            return {"CANCELLED"}
        # Bookmark the server root, not the deep path — matches NVIDIA's UX.
        host = d.replace("omniverse://", "", 1).split("/", 1)[0]
        target = f"omniverse://{host}"
        if add_bookmark(context, target):
            self.report({"INFO"}, f"Bookmarked {target}")
            init_location_list(context, get_bookmarks(context), [])
        else:
            self.report({"WARNING"}, f"Already bookmarked: {target}")
        return {"FINISHED"}


class OMNI_OT_RemoveBookmark(Operator):
    """Remove the selected bookmark from the Bookmarks list."""
    bl_idname = "omni.remove_bookmark"
    bl_label = "Remove Bookmark"

    @classmethod
    def poll(cls, context):
        settings = _settings(context)
        return (0 <= settings.location_list_index < len(settings.location_list))

    def execute(self, context):
        settings = _settings(context)
        target = settings.location_list[settings.location_list_index].name
        if not remove_bookmark(context, target):
            self.report({"WARNING"}, f"Not a saved bookmark: {target}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Removed bookmark {target}")
        init_location_list(context, get_bookmarks(context), event_handlers.g_open_servers)
        return {"FINISHED"}


class OMNI_OT_ClearConnectionStatus(Operator):
    """Clear the most recent connection-status report."""
    bl_idname = "omni.clear_connection_status"
    bl_label = "Clear Status"

    def execute(self, context):
        _settings(context).connection_status_report = ""
        return {"FINISHED"}


# --- auth-prompt modal popup ----------------------------------------------

class OMNI_OT_AuthPrompt(Operator):
    """Modal popup that surfaces a device-flow auth challenge from the
    sidecar. Tells the user the URL + code to enter in their browser; lets
    them cancel the auth attempt."""
    bl_idname = "omni.auth_prompt"
    bl_label = "Nucleus Authentication"

    server: StringProperty(default="")
    url: StringProperty(default="")
    code: StringProperty(default="")
    auth_handle: IntProperty(default=0)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text=f"Server: {self.server}", icon="WORLD")
        col.separator()
        col.label(text="To sign in, open this URL in a browser:")
        col.prop(self, "url", text="")
        col.label(text="Enter this code:")
        col.prop(self, "code", text="")
        col.separator()
        col.label(text="(Press OK to wait, Cancel to abort the attempt.)")

    def execute(self, context):
        # OK button = "I've started; keep waiting." We do nothing; the
        # sidecar will fire connection_status=CONNECTED when it succeeds.
        return {"FINISHED"}

    def cancel(self, context):
        if self.auth_handle:
            try:
                get_client().call_blocking(
                    "auth_cancel", auth_handle=int(self.auth_handle), timeout=5.0
                )
            except Exception as exc:
                print(f"[bna] auth_cancel failed: {exc}")
        _settings(context).auth_prompt_active = False


# --- Phase 5: open / save USD --------------------------------------------

# bpy.app.tempdir is a session-scoped directory Blender deletes on exit.
# Putting all our scratch files in a subdirectory makes cleanup trivial and
# keeps imports from different sessions from colliding with each other.
def _temp_dir() -> Path:
    base = Path(bpy.app.tempdir) / "blender_nucleus_addon"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ensure_cache_root() -> Path:
    """Point usd_deps at our session-scoped temp dir. Idempotent."""
    root = _temp_dir() / "cache"
    root.mkdir(parents=True, exist_ok=True)
    usd_deps.set_cache_root(root)
    return root


def _pull_one_blocking(url: str, timeout: float = 120.0) -> bytes:
    """Synchronous read_file used during recursive dependency pulls.
    Decodes the sidecar's base64 result. Raises on error."""
    result = get_client().call_blocking("read_file", url=url, timeout=timeout)
    return base64.b64decode(result["content_b64"], validate=True)


class OMNI_OT_ImportUSD(Operator):
    """Download the selected file from Nucleus and import it into the current scene."""
    bl_idname = "omni.import_usd"
    bl_label = "Open from Nucleus"
    bl_options = {"REGISTER", "UNDO"}

    # SKIP_SAVE on url so each panel click starts from "" and re-reads the
    # current selection — otherwise Blender remembers the previous Open's
    # URL and ignores the new selection.
    url: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    # Set internally when chaining through the save-then-open path so the
    # second Open doesn't re-prompt for replacement.
    skip_prompt: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    # User's choice in the replace-current-stage dialog.
    replace_mode: EnumProperty(
        name="If a stage is already loaded",
        items=[
            ("SAVE_FIRST", "Save First",
             "Push the loaded stage to Nucleus, then replace it with the new one"),
            ("DISCARD", "Discard",
             "Drop the loaded stage without saving, then load the new one"),
        ],
        default="SAVE_FIRST",
        options={"HIDDEN", "SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context):
        s = _settings(context)
        if s.transfer_active:
            return False
        idx = s.file_list_index
        if idx < 0 or idx >= len(s.file_list):
            return False
        item = s.file_list[idx]
        return (not item.is_directory) and item.is_accessible

    def invoke(self, context, event):
        s = _settings(context)
        if not self.url:
            idx = s.file_list_index
            if idx < 0 or idx >= len(s.file_list):
                self.report({"ERROR"}, "Select a file first")
                return {"CANCELLED"}
            item = s.file_list[idx]
            if item.is_directory:
                self.report({"ERROR"}, "Selected entry is a folder")
                return {"CANCELLED"}
            self.url = s.directory.rstrip("/") + "/" + item.name

        # Single-stage policy: opening a new main USD replaces the loaded
        # one. If a stage is already bound, ask the user whether to save
        # it first or discard it. The chained save-then-open path passes
        # skip_prompt=True so we don't re-prompt after the save finishes.
        if s.source_url and not self.skip_prompt:
            return context.window_manager.invoke_props_dialog(self, width=480)

        # If we got past the prompt with a stage still loaded (i.e. we're
        # the chained Open after a successful Save), wipe the scene before
        # importing so the new stage drops onto a clean slate.
        if s.source_url:
            _clear_loaded_stage(context)

        client = get_client()
        try:
            client.call_blocking("initialize")
        except (RpcError, TimeoutError) as exc:
            self.report({"ERROR"}, f"sidecar init failed: {exc}")
            return {"CANCELLED"}

        self._error: RpcError | None = None
        self._result = None
        self._state = "downloading"

        s.transfer_active = True
        s.transfer_kind = "read"
        s.transfer_url = self.url
        s.transfer_percent = 0

        client.call_async("read_file", self._on_result, url=self.url)

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.15, window=context.window)
        wm.modal_handler_add(self)
        self.report({"INFO"}, f"Downloading {self.url} …")
        return {"RUNNING_MODAL"}

    def draw(self, context):
        s = _settings(context)
        col = self.layout.column()
        col.label(text="A USD stage is already loaded:", icon="ERROR")
        col.label(text=f"   {s.source_url}")
        col.separator()
        col.label(text="Opening a new stage will replace everything in the scene:")
        col.label(text=f"   {self.url}")
        col.separator()
        col.label(text="Save the loaded stage to Nucleus first, or discard it?")
        col.prop(self, "replace_mode", expand=True)

    def execute(self, context):
        s = _settings(context)
        target_url = self.url

        if self.replace_mode == "SAVE_FIRST":
            # Queue the new URL so the export's modal can chain into Open
            # after the upload completes (or clear on failure).
            s.pending_open_url = target_url
            try:
                bpy.ops.omni.export_usd("INVOKE_DEFAULT", use_source_url=True)
            except Exception as exc:
                s.pending_open_url = ""
                self.report({"ERROR"}, f"could not start save: {exc}")
                return {"CANCELLED"}
            self.report({"INFO"}, f"Saving current stage; will open {target_url} after.")
            return {"FINISHED"}

        # DISCARD: re-invoke Open with skip_prompt=True; that path wipes
        # the scene before pulling.
        bpy.ops.omni.import_usd(
            "INVOKE_DEFAULT", url=target_url, skip_prompt=True,
        )
        return {"FINISHED"}

    def _on_result(self, result, error):
        self._result = result
        self._error = error
        self._state = "ready"

    def _finish(self, context, status: set):
        wm = context.window_manager
        if getattr(self, "_timer", None):
            try: wm.event_timer_remove(self._timer)
            except Exception: pass
            self._timer = None
        s = _settings(context)
        s.transfer_active = False
        s.transfer_percent = 0
        return status

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._state != "ready":
            return {"RUNNING_MODAL"}

        if self._error is not None:
            self.report({"ERROR"}, f"read_file failed: {self._error}")
            return self._finish(context, {"CANCELLED"})

        try:
            data = base64.b64decode(self._result["content_b64"], validate=True)
        except Exception as exc:
            self.report({"ERROR"}, f"could not decode response: {exc}")
            return self._finish(context, {"CANCELLED"})

        # Pull the main file plus every external file it (recursively)
        # references — sublayers, payloads, textures — into a cache that
        # mirrors the server tree, so relative refs resolve naturally.
        # Then rewrite any absolute omniverse:// paths in the local copy
        # to point at their cached files (Blender's stock USD has no
        # omniverse:// resolver).
        _ensure_cache_root()
        try:
            walk = usd_deps.walk_and_pull(
                root_url=self.url,
                root_bytes=data,
                pull_one=_pull_one_blocking,
                log=lambda m: print(f"[bna] {m}"),
            )
        except Exception as exc:
            self.report({"ERROR"}, f"dependency walk failed: {exc}")
            return self._finish(context, {"CANCELLED"})

        # Build url -> local_path map for path rewriting and persist the
        # manifest so the export side can hash-diff against it later.
        url_to_local = {url: Path(e.local_path) for url, e in walk.manifest.files.items()}
        try:
            usd_deps.save_manifest(walk.manifest)
        except OSError as exc:
            print(f"[bna] manifest save failed (non-fatal): {exc!r}")

        # Localize: rewrite omniverse:// paths inside the .usd to local
        # absolute paths. No-op for files that already use relative refs.
        try:
            n_rewritten = usd_deps.localize(walk.root_local_path, url_to_local)
        except Exception as exc:
            print(f"[bna] localize raised (non-fatal, continuing): {exc!r}")
            n_rewritten = 0

        # Neutralize UsdUVTexture nodes whose file input is empty or
        # unresolvable. Omniverse uses the texture's `fallback` value as
        # the material's adjustable default; Blender would otherwise import
        # an Image Texture with no image and render black.
        try:
            n_neutralized = usd_deps.neutralize_missing_textures(walk.root_local_path)
            if n_neutralized:
                print(f"[bna] neutralized {n_neutralized} unwired texture input(s)")
        except Exception as exc:
            print(f"[bna] neutralize_missing_textures raised (non-fatal): {exc!r}")

        s = _settings(context)
        s.source_url = self.url
        s.temp_filepath = str(walk.root_local_path)

        try:
            bpy.ops.wm.usd_import(
                filepath=str(walk.root_local_path),
                merge_parent_xform=False,
            )
        except Exception as exc:
            self.report({"ERROR"}, f"usd_import failed: {exc}")
            return self._finish(context, {"CANCELLED"})

        size_kb = len(data) / 1024.0
        if walk.failed:
            self.report(
                {"WARNING"},
                f"Imported {self.url} ({size_kb:.1f} KB main, "
                f"{len(walk.pulled)} deps pulled, {len(walk.failed)} failed)",
            )
        else:
            self.report(
                {"INFO"},
                f"Imported {self.url} ({size_kb:.1f} KB main, "
                f"{len(walk.pulled)} deps pulled, {len(walk.skipped_cached)} cached, "
                f"{n_rewritten} paths rewritten)",
            )
        return self._finish(context, {"FINISHED"})


class OMNI_OT_ExportUSD(Operator):
    """Export the current Blender scene to USD and upload it to Nucleus."""
    bl_idname = "omni.export_usd"
    bl_label = "Save to Nucleus"
    bl_options = {"REGISTER"}

    target_url: StringProperty(
        name="Target URL",
        description="omniverse:// URL to write to.",
        default="",
    )
    # When True, invoke() short-circuits straight to execute() using the
    # source_url stashed on the scene. Cleared by the "Save As…" button so
    # the URL prompt always shows.
    use_source_url: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    # When True, pass texture-export options to wm.usd_export so the user's
    # local textures get copied alongside the .usd into the cache mirror,
    # where the dep-walk + upload loop below can find them. Used by the
    # "New" button — round-trip Save/Save-As keeps the default False because
    # the textures are already in the cache mirror from the prior Open.
    is_new_file: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    # Export filters mirroring wm.usd_export property names so they can be
    # splatted straight into the kwargs. Anything this Blender build doesn't
    # support is filtered out in execute() via the supported-set check.
    # World Dome Light defaults off — this addon targets asset round-trips,
    # and a stray dome light is the kind of thing that quietly corrupts an
    # asset on save (the bug that motivated exposing this dialog).
    selection_only: BoolProperty(name="Selection Only", default=False)
    export_animation: BoolProperty(name="Animation", default=False)
    export_meshes: BoolProperty(name="Meshes", default=True)
    export_lights: BoolProperty(name="Lights", default=True)
    export_cameras: BoolProperty(name="Cameras", default=True)
    export_curves: BoolProperty(name="Curves", default=True)
    export_points: BoolProperty(name="Point Clouds", default=True)
    export_volumes: BoolProperty(name="Volumes", default=True)
    export_hair: BoolProperty(name="Hair", default=False)
    convert_world_material: BoolProperty(name="World Dome Light", default=False)
    export_materials: BoolProperty(name="Materials", default=True)
    export_uvmaps: BoolProperty(name="UV Maps", default=True)
    export_normals: BoolProperty(name="Normals", default=True)

    @classmethod
    def poll(cls, context):
        return not _settings(context).transfer_active

    def invoke(self, context, event):
        s = _settings(context)
        if self.use_source_url and s.source_url:
            self.target_url = s.source_url
        elif not self.target_url:
            if s.directory and s.filename:
                self.target_url = s.directory.rstrip("/") + "/" + s.filename
            elif s.directory:
                self.target_url = s.directory
            else:
                self.target_url = "omniverse://"
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        s = _settings(context)
        layout = self.layout
        col = layout.column()
        col.label(text="Target Nucleus URL:")
        col.prop(self, "target_url", text="")
        col.separator()
        row = col.row(align=True)
        row.prop(s, "set_checkpoint_message", text="")
        sub = row.column()
        sub.enabled = s.set_checkpoint_message
        sub.prop(s, "checkpoint_message", text="Checkpoint message")

        layout.separator()
        box = layout.box()
        box.label(text="General")
        box.prop(self, "selection_only")
        box.prop(self, "export_animation")

        box = layout.box()
        box.label(text="Object Types")
        grid = box.grid_flow(row_major=True, columns=2, even_columns=True)
        grid.prop(self, "export_meshes")
        grid.prop(self, "export_lights")
        grid.prop(self, "convert_world_material")
        grid.prop(self, "export_cameras")
        grid.prop(self, "export_curves")
        grid.prop(self, "export_points")
        grid.prop(self, "export_volumes")
        grid.prop(self, "export_hair")

        box = layout.box()
        box.label(text="Geometry / Materials")
        box.prop(self, "export_uvmaps")
        box.prop(self, "export_normals")
        box.prop(self, "export_materials")

    def execute(self, context):
        s = _settings(context)
        url = self.target_url.strip()
        if not url.startswith("omniverse://"):
            self.report({"ERROR"}, "Target URL must start with omniverse://")
            return {"CANCELLED"}
        if url.endswith("/"):
            self.report({"ERROR"}, "Target URL must point at a file, not a folder")
            return {"CANCELLED"}

        # Local export target: cache-mirror path so dependency files
        # (textures, sublayers) are written into the same tree we pulled
        # them from on import. Falls back to a hash-derived path for
        # non-omniverse:// URLs.
        _ensure_cache_root()
        local_main = usd_deps.cache_path_for_url(url)
        local_main.parent.mkdir(parents=True, exist_ok=True)

        # Pre-export manifest tells us what was pulled (and at which hash)
        # so we can diff after the export and only push what changed.
        # Missing manifest just means "everything looks new" — we'll push
        # whatever ends up under the cache mirror.
        manifest = usd_deps.load_manifest(url) or usd_deps.Manifest(root_url=url)

        try:
            export_kwargs = {"filepath": str(local_main)}
            # Filter by what this Blender's usd_export actually supports —
            # property names drift between versions, and we'd rather export
            # without an unrecognized flag than fail outright.
            try:
                supported = set(bpy.ops.wm.usd_export.get_rna_type().properties.keys())
            except Exception:
                supported = set()

            for name in (
                "selection_only", "export_animation",
                "export_meshes", "export_lights", "export_cameras",
                "export_curves", "export_points", "export_volumes", "export_hair",
                "convert_world_material",
                "export_materials", "export_uvmaps", "export_normals",
            ):
                if name in supported:
                    export_kwargs[name] = getattr(self, name)

            if self.is_new_file:
                # Copy textures next to the .usd in the cache mirror so the
                # dep-walk below picks them up via local_path_to_url().
                # Without these flags, brand-new exports skip textures
                # (they live on the user's disk, not under the cache mirror).
                for opt, val in (
                    ("export_textures", True),
                    ("overwrite_textures", True),
                    ("relative_paths", True),
                ):
                    if opt in supported:
                        export_kwargs[opt] = val
            bpy.ops.wm.usd_export(**export_kwargs)
        except Exception as exc:
            self.report({"ERROR"}, f"usd_export failed: {exc}")
            return {"CANCELLED"}

        # Walk the freshly-exported .usd to find every file it references.
        try:
            referenced_locals = usd_deps.collect_local_references(local_main)
        except Exception as exc:
            print(f"[bna] dep walk on export raised (non-fatal): {exc!r}")
            referenced_locals = set()

        # Decide which dependencies to push:
        #   - File under the cache mirror with a known omniverse:// URL
        #     AND (not in manifest, or hash changed) → push.
        #   - File outside the cache mirror → warn, skip.
        client = get_client()
        try:
            client.call_blocking("initialize")
        except (RpcError, TimeoutError) as exc:
            self.report({"ERROR"}, f"sidecar init failed: {exc}")
            return {"CANCELLED"}

        comment = s.checkpoint_message if s.set_checkpoint_message else ""

        dep_pushed: list[str] = []
        dep_skipped_unchanged: list[str] = []
        dep_skipped_outside: list[str] = []
        dep_failed: list[tuple[str, str]] = []

        for dep_path in sorted(referenced_locals):
            if not dep_path.exists() or not dep_path.is_file():
                continue
            dep_url = usd_deps.local_path_to_url(dep_path)
            if dep_url is None:
                dep_skipped_outside.append(str(dep_path))
                continue
            try:
                new_hash = usd_deps.compute_hash(dep_path)
            except OSError as exc:
                dep_failed.append((dep_url, repr(exc)))
                continue
            existing = manifest.files.get(dep_url)
            if existing and existing.hash == new_hash:
                dep_skipped_unchanged.append(dep_url)
                continue
            try:
                dep_bytes = dep_path.read_bytes()
                client.call_blocking(
                    "write_file",
                    url=dep_url,
                    content_b64=base64.b64encode(dep_bytes).decode("ascii"),
                    message=comment,
                    timeout=300.0,
                )
            except (RpcError, TimeoutError, OSError) as exc:
                dep_failed.append((dep_url, repr(exc)))
                continue
            manifest.files[dep_url] = usd_deps.FileEntry(
                url=dep_url,
                local_path=str(dep_path).replace("\\", "/"),
                hash=new_hash,
                size=len(dep_bytes),
            )
            dep_pushed.append(dep_url)

        # Build the bytes we'll push for the main .usd: a delocalized copy
        # so the on-server file references omniverse:// URLs, not local
        # absolute paths from this machine. Leave local_main itself
        # untouched so Blender can keep editing it without re-localizing.
        url_to_local = {u: Path(e.local_path) for u, e in manifest.files.items()}
        local_to_url = {Path(e.local_path): u for u, e in manifest.files.items()}
        # Keep the real USD extension at the end so Sdf.Layer.FindOrOpen
        # can detect the format during delocalize. Foo.usd -> Foo.upload.usd.
        upload_path = local_main.with_name(local_main.stem + ".upload" + local_main.suffix)
        try:
            shutil.copy2(local_main, upload_path)
            usd_deps.delocalize(upload_path, local_to_url)
            data = upload_path.read_bytes()
        except Exception as exc:
            self.report({"ERROR"}, f"could not prepare upload bytes: {exc}")
            try: upload_path.unlink()
            except OSError: pass
            return {"CANCELLED"}

        s.transfer_active = True
        s.transfer_kind = "write"
        s.transfer_url = url
        s.transfer_percent = 0

        self._url = url
        self._comment = comment
        self._state = "uploading"
        self._error = None
        self._result = None
        self._size = len(data)
        self._temp_path = str(local_main)
        self._upload_path = upload_path
        self._manifest = manifest
        self._dep_pushed = dep_pushed
        self._dep_skipped_unchanged = dep_skipped_unchanged
        self._dep_skipped_outside = dep_skipped_outside
        self._dep_failed = dep_failed

        client.call_async(
            "write_file",
            self._on_result,
            url=url,
            content_b64=base64.b64encode(data).decode("ascii"),
            message=comment,
        )

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.15, window=context.window)
        wm.modal_handler_add(self)
        self.report(
            {"INFO"},
            f"Uploading {self._size} bytes to {url} "
            f"({len(dep_pushed)} deps changed, {len(dep_skipped_unchanged)} unchanged) …",
        )
        return {"RUNNING_MODAL"}

    def _on_result(self, result, error):
        self._result = result
        self._error = error
        self._state = "ready"

    def _finish(self, context, status: set):
        wm = context.window_manager
        if getattr(self, "_timer", None):
            try: wm.event_timer_remove(self._timer)
            except Exception: pass
            self._timer = None
        s = _settings(context)
        s.transfer_active = False
        s.transfer_percent = 0
        return status

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._state != "ready":
            return {"RUNNING_MODAL"}

        # Whatever the outcome, the .upload sibling we made for delocalize
        # is no longer needed.
        try:
            if getattr(self, "_upload_path", None) and self._upload_path.exists():
                self._upload_path.unlink()
        except OSError:
            pass

        if self._error is not None:
            # A failed Save must not silently trigger a queued Open —
            # the user expected the save to land before we replaced the
            # stage. Drop the chain and let them retry.
            _settings(context).pending_open_url = ""
            self.report({"ERROR"}, f"write_file failed: {self._error}")
            return self._finish(context, {"CANCELLED"})

        # Record the just-pushed main .usd in the manifest so the next
        # save can hash-diff against this snapshot.
        try:
            local_main = Path(self._temp_path)
            self._manifest.files[self._url] = usd_deps.FileEntry(
                url=self._url,
                local_path=str(local_main).replace("\\", "/"),
                hash=usd_deps.compute_hash(local_main),
                size=local_main.stat().st_size,
            )
            self._manifest.root_url = self._url
            usd_deps.save_manifest(self._manifest)
        except Exception as exc:
            print(f"[bna] manifest save after push failed (non-fatal): {exc!r}")

        s = _settings(context)
        s.source_url = self._url
        s.temp_filepath = self._temp_path
        size_kb = self._size / 1024.0
        n_changed = len(self._dep_pushed)
        n_unchanged = len(self._dep_skipped_unchanged)
        n_outside = len(self._dep_skipped_outside)
        n_failed = len(self._dep_failed)
        suffix = (
            f" (deps: {n_changed} pushed, {n_unchanged} unchanged"
            + (f", {n_outside} outside cache" if n_outside else "")
            + (f", {n_failed} failed" if n_failed else "")
            + ")"
        )
        level = "WARNING" if (n_outside or n_failed) else "INFO"
        if self._comment:
            self.report({level}, f"Saved {size_kb:.1f} KB to {self._url} (with checkpoint){suffix}")
        else:
            self.report({level}, f"Saved {size_kb:.1f} KB to {self._url}{suffix}")
        if n_outside:
            print(f"[bna] not pushed (outside cache mirror): {self._dep_skipped_outside}")
        if n_failed:
            print(f"[bna] dep push failures: {self._dep_failed}")

        # If this save was kicked off by "Save First" in the Open-replace
        # dialog, chain into the queued Open now that the upload landed.
        pending = s.pending_open_url
        s.pending_open_url = ""
        status = self._finish(context, {"FINISHED"})
        if pending:
            try:
                bpy.ops.omni.import_usd(
                    "INVOKE_DEFAULT", url=pending, skip_prompt=True,
                )
            except Exception as exc:
                print(f"[bna] chained Open failed: {exc!r}")
        return status


_USD_FILE_EXTS = (".usd", ".usda", ".usdc", ".usdz")


def _file_exists_on_server(folder_url: str, filename: str) -> bool:
    """List the server folder and return True iff filename is present as a file."""
    folder = folder_url if folder_url.endswith("/") else folder_url + "/"
    client = get_client()
    client.call_blocking("initialize")
    result = client.call_blocking("list", url=folder, timeout=20.0)
    for entry in result.get("entries", []):
        if entry.get("is_dir"):
            continue
        if entry.get("relative_path") == filename:
            return True
    return False


def _kick_off_new_upload(op, target_url: str):
    try:
        bpy.ops.omni.export_usd(
            "EXEC_DEFAULT",
            target_url=target_url,
            use_source_url=False,
            is_new_file=True,
        )
    except Exception as exc:
        op.report({"ERROR"}, f"could not start upload: {exc}")
        return {"CANCELLED"}
    return {"FINISHED"}


class OMNI_OT_NewFile(Operator):
    """Save the current Blender scene as a new USD file in the selected Nucleus folder."""
    bl_idname = "omni.new_file"
    bl_label = "New File on Nucleus"

    filename: StringProperty(
        name="File Name",
        description="USD file name to create in the current directory",
        default="untitled.usd",
    )

    @classmethod
    def poll(cls, context):
        s = _settings(context)
        if s.transfer_active:
            return False
        d = (s.directory or "").strip()
        return d.startswith("omniverse://")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        s = _settings(context)
        col = self.layout.column()
        col.label(text="Target folder:", icon="FILE_FOLDER")
        col.label(text=f"   {s.directory}")
        col.separator()
        col.label(text="New USD file name:")
        col.prop(self, "filename", text="")

    def execute(self, context):
        s = _settings(context)
        directory = (s.directory or "").strip()
        if not directory.startswith("omniverse://"):
            self.report({"ERROR"}, "Directory must be an omniverse:// URL")
            return {"CANCELLED"}

        name = self.filename.strip().strip("/\\")
        if not name:
            self.report({"ERROR"}, "File name is empty")
            return {"CANCELLED"}
        if "/" in name or "\\" in name:
            self.report({"ERROR"}, "File name may not contain slashes")
            return {"CANCELLED"}
        if not name.lower().endswith(_USD_FILE_EXTS):
            name = name + ".usd"

        target_url = directory.rstrip("/") + "/" + name

        try:
            collision = _file_exists_on_server(directory, name)
        except (RpcError, TimeoutError) as exc:
            self.report({"ERROR"}, f"could not check folder: {exc}")
            return {"CANCELLED"}

        if collision:
            try:
                bpy.ops.omni.confirm_new_file_overwrite(
                    "INVOKE_DEFAULT",
                    target_url=target_url,
                )
            except Exception as exc:
                self.report({"ERROR"}, f"could not open confirm dialog: {exc}")
                return {"CANCELLED"}
            return {"FINISHED"}

        return _kick_off_new_upload(self, target_url)


class OMNI_OT_ConfirmNewFileOverwrite(Operator):
    """Confirmation dialog shown when 'New' targets a file that already exists."""
    bl_idname = "omni.confirm_new_file_overwrite"
    bl_label = "Overwrite existing file?"

    target_url: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=480)

    def draw(self, context):
        col = self.layout.column()
        col.label(text="A file with this name already exists on the server:", icon="ERROR")
        col.label(text=f"   {self.target_url}")
        col.separator()
        col.label(text="OK = overwrite (a Nucleus checkpoint will record the change)")
        col.label(text="Cancel = keep the existing file untouched")

    def execute(self, context):
        return _kick_off_new_upload(self, self.target_url)


class OMNI_OT_ClearSourceUrl(Operator):
    """Clear the stashed source URL — useful when you want Save to prompt for a new path."""
    bl_idname = "omni.clear_source_url"
    bl_label = "Clear Source URL"

    @classmethod
    def poll(cls, context):
        return bool(_settings(context).source_url)

    def execute(self, context):
        s = _settings(context)
        s.source_url = ""
        s.temp_filepath = ""
        return {"FINISHED"}


# --- diagnostics ----------------------------------------------------------

class OMNI_OT_RestartSidecar(Operator):
    """Restart the sidecar process (for recovering from a sidecar crash)."""
    bl_idname = "omni.restart_sidecar"
    bl_label = "Restart Sidecar"

    def execute(self, context):
        from . import rpc_client
        try:
            rpc_client.shutdown_client()
            rpc_client.get_client()  # respawn
        except Exception as exc:
            self.report({"ERROR"}, f"restart failed: {exc}")
            return {"CANCELLED"}
        _settings(context).sidecar_status = "running"
        self.report({"INFO"}, "Sidecar restarted")
        return {"FINISHED"}
