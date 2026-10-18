--[[ Import-from-list plugin: adds photos to the catalog IN PLACE from a
     plain-text file of absolute paths (one per line), no copying. Built for
     the friends-raws "photos of me" flow but generic.

     "Import Photos of Me" re-imports one remembered list file with no picker,
     so a single ever-growing list keeps topping up a single collection. ]]
local items = {
    {
        title = 'Import Photos of Me',
        file = 'ImportSaved.lua',
    },
    {
        title = 'Import photos from list file…',
        file = 'ImportList.lua',
    },
    {
        title = 'Choose Photos of Me list…',
        file = 'ChooseSaved.lua',
    },
}

return {
    LrSdkVersion = 6.0,
    LrToolkitIdentifier = 'com.ryanmcl.importlist',
    LrPluginName = 'Import from List File',
    -- both menus so it's findable from File > Plug-in Extras too
    LrExportMenuItems = items,
    LrLibraryMenuItems = items,
    VERSION = { major = 1, minor = 1, revision = 0 },
}
