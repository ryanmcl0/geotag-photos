local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'
local LrProgressScope = import 'LrProgressScope'

LrTasks.startAsyncTask(function()
    local files = LrDialogs.runOpenPanel {
        title = 'Choose a list file (one absolute photo path per line)',
        canChooseFiles = true,
        canChooseDirectories = false,
        allowsMultipleSelection = false,
        fileTypes = { 'txt' },
    }
    if not files or not files[1] then return end
    local listPath = files[1]

    local content = LrFileUtils.readFile(listPath)
    if not content then
        LrDialogs.message('Could not read ' .. listPath)
        return
    end
    local paths = {}
    for line in string.gmatch(content, '[^\r\n]+') do
        line = line:match('^%s*(.-)%s*$')
        if line ~= '' then paths[#paths + 1] = line end
    end

    local catalog = LrApplication.activeCatalog()
    local collectionName = LrPathUtils.removeExtension(
        LrPathUtils.leafName(listPath))
    local added, already, missing, failed, errors = {}, 0, 0, 0, {}

    local progress = LrProgressScope { title = 'Importing from list…' }
    -- chunked write access: one giant block makes LR unresponsive
    local CHUNK = 50
    for start = 1, #paths, CHUNK do
        catalog:withWriteAccessDo('Import from list (' .. start .. ')', function()
            for i = start, math.min(start + CHUNK - 1, #paths) do
                local p = paths[i]
                if not LrFileUtils.exists(p) then
                    missing = missing + 1
                else
                    local existing = catalog:findPhotoByPath(p)
                    if existing then
                        already = already + 1
                        added[#added + 1] = existing
                    else
                        local ok, photoOrErr = pcall(function()
                            return catalog:addPhoto(p)
                        end)
                        if ok and photoOrErr then
                            added[#added + 1] = photoOrErr
                        else
                            failed = failed + 1
                            errors[#errors + 1] = p .. ' :: ' .. tostring(photoOrErr)
                        end
                    end
                end
            end
        end, { timeout = 60 })
        progress:setPortionComplete(math.min(start + CHUNK - 1, #paths), #paths)
        if progress:isCanceled() then break end
    end

    catalog:withWriteAccessDo('Collect list imports', function()
        local coll = catalog:createCollection(collectionName, nil, true)
        if coll then coll:addPhotos(added) end
    end, { timeout = 60 })
    progress:done()

    if #errors > 0 then
        local errPath = LrPathUtils.replaceExtension(listPath, 'errors.txt')
        local f = io.open(errPath, 'w')
        if f then
            f:write(table.concat(errors, '\n'))
            f:close()
        end
    end

    LrDialogs.message('Import from list finished',
        string.format('%d added, %d were already in the catalog, %d missing, %d failed.\nAll grouped in collection "%s".',
            #added - already, already, missing, failed, collectionName))
end)
