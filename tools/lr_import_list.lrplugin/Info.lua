--[[ Import-from-list plugin: adds photos to the catalog IN PLACE from a
     plain-text file of absolute paths (one per line), no copying. Built for
     the friends-raws "photos of me" flow but generic. ]]
return {
    LrSdkVersion = 6.0,
    LrToolkitIdentifier = 'com.ryanmcl.importlist',
    LrPluginName = 'Import from List File',
    LrLibraryMenuItems = {
        {
            title = 'Import photos from list file…',
            file = 'ImportList.lua',
        },
    },
    VERSION = { major = 1, minor = 0, revision = 0 },
}
