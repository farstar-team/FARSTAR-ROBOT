"""One bounded in-memory receipt upload at a time; never spool receipt photos to disk."""
from contextlib import asynccontextmanager

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

MAX_BODY = 6 * 1024 * 1024
MAX_PHOTO = 5 * 1024 * 1024


class ReceiptFields(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: int = Field(ge=10000, le=100000000)
    request_id: str = Field(min_length=8, max_length=80)
    order_id: str = Field(min_length=8, max_length=40)


class MemoryReceiptParser(MultiPartParser):
    spool_max_size = MAX_BODY + 1


@asynccontextmanager
async def receipt_form(request):
    if not request.headers.get('content-type', '').lower().startswith('multipart/form-data;'):
        raise HTTPException(422, 'عکس فیش را از فرم ارسال تصویر انتخاب کن.')
    async def limited():
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                raise HTTPException(413, 'حجم درخواست بیش از حد مجاز است.')
            yield chunk
    parser = MemoryReceiptParser(request.headers, limited(), max_files=1, max_fields=3, max_part_size=1024)
    form = None
    try:
        form = await parser.parse()
        if len(form.multi_items()) != 4 or set(form) != {'photo', 'amount', 'order_id', 'request_id'}:
            raise HTTPException(422, 'فیلدهای رسید معتبر نیست.')
        photo = form['photo']
        if not isinstance(photo, UploadFile) or photo.size is None or photo.size > MAX_PHOTO:
            raise HTTPException(413, 'حجم عکس فیش حداکثر ۵ مگابایت است.')
        fields = ReceiptFields.model_validate({k: form[k] for k in ('amount', 'order_id', 'request_id')})
        yield fields, await photo.read()
    except (MultiPartException, ValidationError, ValueError) as exc:
        raise HTTPException(422, 'عکس فیش و مشخصات سفارش را درست انتخاب کن.') from exc
    finally:
        if form is not None:
            await form.close()
        # Also close partial files on body-limit errors or cancellation before parsing finishes.
        for file in parser._files_to_close_on_error:
            file.close()
