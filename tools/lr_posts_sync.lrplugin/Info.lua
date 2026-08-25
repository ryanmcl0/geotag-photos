--[[
Posts Sync — one-click import of every smart collection that ./post.py pull
writes into /Volumes/RYAN/Edits/Posts/<post>/<post>.lrsmcol.

Install once: Lightroom Classic > File > Plug-in Manager > Add > pick this
.lrplugin folder. It syncs automatically shortly after every Lightroom
launch, and Library > Plug-in Extras > "Sync Post Collections" does the same
on demand. Both mirror every post into a smart collection inside the "Posts"
collection set (creating new ones, updating changed ones - safe to run any
time).
]]

return {
    LrSdkVersion = 6.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = 'com.ryanmcl0.posts-sync',
    LrPluginName = 'Posts Sync',
    LrInitPlugin = 'Init.lua',
    LrLibraryMenuItems = {
        { title = 'Sync Post Collections', file = 'SyncAll.lua' },
    },
    VERSION = { major = 1, minor = 0, revision = 0 },
}
