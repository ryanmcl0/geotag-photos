--[[ Import-from-list plugin: adds photos to the catalog IN PLACE from a
     plain-text file of absolute paths (one per line), no copying.

     One menu item, "Import Photos of Me": re-imports one remembered list
     file with no picker, so a single ever-growing list keeps topping up a
     single collection. It asks for the file on first use, or again if the
     saved file is gone. ]]
local items = {
    {
        title = 'Import Photos of Me',
        file = 'ImportSaved.lua',
    },
}

return {
    LrSdkVersion = 6.0,
    -- unchanged identifier so the remembered list file survives the rename
    LrToolkitIdentifier = 'com.ryanmcl.importlist',
    LrPluginName = 'Photos of Me',
    -- both menus so it's findable from File > Plug-in Extras too
    LrExportMenuItems = items,
    LrLibraryMenuItems = items,
    VERSION = { major = 1, minor = 2, revision = 0 },
}
