# SketchUp importer for Blender

Windows x64, Blender 4.2 or later. This standalone add-on also works with standard Blender builds.

1. Download the `skp-importer` artifact from the **Package SketchUp importer** workflow in GitHub Actions.
2. In Blender, open **Edit → Preferences → Add-ons → Install from Disk**, select the downloaded ZIP, and enable **SketchUp (.skp)**.
3. Download and extract the [SketchUp Desktop SDK](https://extensions.sketchup.com/en/developer_center/sketchup_sdk). Use the latest Windows x64 SDK. In the add-on preferences, select `SketchUpAPI.dll` from the SDK's `binaries/sketchup/x64` directory. Keep adjacent runtime DLLs in place.
4. Open **File → Import → SketchUp (.skp)**.

**Hierarchy** creates one mesh object per group or instance and parents nested objects. **Join** bakes descendants into their top-level component mesh. **Automatic** joins an entire subtree when a component has at least the specified number of immediate child groups and instances. The default is 10.

The importer reads geometry in inches and converts it to metres. Faces without holes retain their outer polygon. Faces with holes use SketchUp's triangulation. Imported materials use a Principled shader with a single packed color texture. Blender faces have one material slot, so the importer uses the front material when both sides are painted and the back material when only the back is painted.

File compatibility comes from the installed SDK. If it reports that the file is newer, the importer warns that it may omit data. Perspective-projected and inherited texture placement may differ from SketchUp. The SDK runtime is supplied separately.
