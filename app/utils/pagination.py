from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

async def paginate(db: AsyncSession, query, page=1, per_page=25):
    offset = (page - 1) * per_page
    
    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0
    
    # Get items
    items = (await db.execute(query.offset(offset).limit(per_page))).scalars().all()
    
    pages = (total + per_page - 1) // per_page
    
    return {
        'items': items,
        'page': page,
        'per_page': per_page,
        'total': total,
        'pages': pages,
        'has_prev': page > 1,
        'has_next': page < pages,
        'prev_num': page - 1 if page > 1 else None,
        'next_num': page + 1 if page < pages else None,
    }