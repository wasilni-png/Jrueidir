import os
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
import asyncio

import aiohttp
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from telegram.constants import ParseMode

# ============= إعدادات التسجيل =============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============= حالات المحادثة للمستخدم =============
USER_START, USER_LOCATION, USER_DESTINATION, USER_VEHICLE_TYPE, USER_CONFIRM_RIDE, USER_RATING = range(6)

# ============= حالات المحادثة للسائق =============
DRIVER_START, DRIVER_ACTIVE, DRIVER_ACCEPT_RIDE, DRIVER_ON_TRIP = range(4)

# ============= فئات المركبات والأسعار =============
VEHICLE_TYPES = {
    'economy': {'name': 'اقتصادي', 'price_multiplier': 1.0},
    'family': {'name': 'عائلي', 'price_multiplier': 1.3},
    'luxury': {'name': 'فاخر', 'price_multiplier': 1.8}
}

# ============= قاعدة بيانات بسيطة (في الإنتاج استخدم PostgreSQL/MySQL) =============
class Database:
    def __init__(self):
        self.users = {}
        self.drivers = {}
        self.rides = {}
        self.ride_counter = 1
    
    def add_user(self, user_id, user_data):
        self.users[user_id] = user_data
    
    def add_driver(self, driver_id, driver_data):
        self.drivers[driver_id] = driver_data
    
    def create_ride(self, ride_data):
        ride_id = self.ride_counter
        self.rides[ride_id] = {**ride_data, 'status': 'searching'}
        self.ride_counter += 1
        return ride_id
    
    def update_ride(self, ride_id, updates):
        if ride_id in self.rides:
            self.rides[ride_id].update(updates)

db = Database()

# ============= خدمة الخرائط باستخدام OpenStreetMap =============
class MapService:
    @staticmethod
    async def get_static_map(lat: float, lon: float, zoom: int = 15, width: int = 400, height: int = 300) -> str:
        """إنشاء رابط لخريطة ثابتة من OSM"""
        base_url = "https://staticmap.openstreetmap.de/staticmap.php"
        params = {
            'center': f'{lat},{lon}',
            'zoom': zoom,
            'size': f'{width}x{height}',
            'markers': f'{lat},{lon},red-pushpin',
            'maptype': 'mapnik'
        }
        return f"{base_url}?{'&'.join(f'{k}={v}' for k,v in params.items())}"
    
    @staticmethod
    async def get_route_map(start_lat: float, start_lon: float, 
                           end_lat: float, end_lon: float) -> str:
        """إنشاء رابط لخريطة مسار باستخدام OSM"""
        base_url = "https://staticmap.openstreetmap.de/staticmap.php"
        params = {
            'center': f'{(start_lat+end_lat)/2},{(start_lon+end_lon)/2}',
            'zoom': 13,
            'size': '600x400',
            'markers': f'{start_lat},{start_lon},green-pushpin|{end_lat},{end_lon},red-pushpin',
            'maptype': 'mapnik'
        }
        return f"{base_url}?{'&'.join(f'{k}={v}' for k,v in params.items())}"
    
    @staticmethod
    async def geocode_address(address: str) -> Tuple[Optional[float], Optional[float]]:
        """تحويل العنوان إلى إحداثيات باستخدام Nominatim"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
                headers = {'User-Agent': 'RideSharingBot/1.0'}
                
                async with session.get(url, headers=headers) as response:
                    data = await response.json()
                    if data:
                        return float(data[0]['lat']), float(data[0]['lon'])
        except Exception as e:
            logger.error(f"Geocoding error: {e}")
        return None, None

# ============= دوال المستخدم =============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء المحادثة"""
    user = update.effective_user
    
    # حفظ بيانات المستخدم
    db.add_user(user.id, {
        'username': user.username,
        'first_name': user.first_name,
        'balance': 1000.0  # رصيد افتراضي
    })
    
    keyboard = [
        [KeyboardButton("🚖 طلب مشوار")],
        [KeyboardButton("👤 إدارة الحساب"), KeyboardButton("❓ مساعدة")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"🎉 أهلاً بك {user.first_name} في خدمة طلب المشاوير!\n\n"
        "يمكنك اختيار أحد الخيارات التالية:",
        reply_markup=reply_markup
    )
    
    return USER_START

async def request_ride(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء طلب مشوار جديد"""
    context.user_data.clear()
    
    await update.message.reply_text(
        "📍 **الخطوة 1: تحديد موقع الانطلاق**\n\n"
        "يرجى إرسال موقعك الحالي عبر زر مشاركة الموقع أو كتابة العنوان النصي.",
        reply_markup=ReplyKeyboardRemove()
    )
    
    return USER_LOCATION

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة موقع الانطلاق"""
    if update.message.location:
        # إذا أرسل الموقع مباشرة
        location = update.message.location
        context.user_data['pickup_location'] = {
            'lat': location.latitude,
            'lon': location.longitude,
            'type': 'gps'
        }
    elif update.message.text:
        # إذا أرسل العنوان نصياً
        address = update.message.text
        lat, lon = await MapService.geocode_address(address)
        
        if lat and lon:
            context.user_data['pickup_location'] = {
                'lat': lat,
                'lon': lon,
                'address': address,
                'type': 'text'
            }
        else:
            await update.message.reply_text(
                "❌ لم أتمكن من تحديد هذا الموقع. يرجى المحاولة مرة أخرى أو إرسال موقع GPS."
            )
            return USER_LOCATION
    
    # تأكيد الموقع
    loc = context.user_data['pickup_location']
    map_url = await MapService.get_static_map(loc['lat'], loc['lon'])
    
    keyboard = [
        [
            InlineKeyboardButton("✅ نعم، تأكيد الموقع", callback_data='confirm_location'),
            InlineKeyboardButton("✏️ تعديل", callback_data='edit_location')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_photo(
        photo=map_url,
        caption=f"📍 **تم تحديد موقع الانطلاق**\n\n"
                f"الإحداثيات: {loc['lat']:.4f}, {loc['lon']:.4f}\n"
                f"{'العنوان: ' + loc.get('address', '') if 'address' in loc else ''}\n\n"
                f"هل هذا الموقع صحيح؟",
        reply_markup=reply_markup
    )
    
    return USER_DESTINATION

async def location_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ردود تأكيد الموقع"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'edit_location':
        await query.edit_message_caption(
            caption="✏️ يرجى إعادة إرسال موقع الانطار:"
        )
        return USER_LOCATION
    
    # تأكيد الموقع والانتقال للوجهة
    await query.edit_message_caption(
        caption="✅ **تم تأكيد موقع الانطلاق**\n\n"
                "📍 **الخطوة 2: تحديد الوجهة**\n"
                "يرجى كتابة عنوان الوجهة المطلوبة:"
    )
    
    return USER_DESTINATION

async def handle_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الوجهة"""
    if not update.message.text:
        await update.message.reply_text("يرجى إرسال العنوان نصياً.")
        return USER_DESTINATION
    
    address = update.message.text
    lat, lon = await MapService.geocode_address(address)
    
    if not lat or not lon:
        await update.message.reply_text(
            "❌ لم أتمكن من تحديد هذا العنوان. يرجى المحاولة مرة أخرى."
        )
        return USER_DESTINATION
    
    context.user_data['destination'] = {
        'lat': lat,
        'lon': lon,
        'address': address
    }
    
    # تأكيد الوجهة
    dest = context.user_data['destination']
    map_url = await MapService.get_static_map(dest['lat'], dest['lon'])
    
    keyboard = [
        [
            InlineKeyboardButton("✅ تأكيد الوجهة", callback_data='confirm_destination'),
            InlineKeyboardButton("✏️ تعديل", callback_data='edit_destination')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_photo(
        photo=map_url,
        caption=f"🏁 **الوجهة المحددة**\n\n"
                f"العنوان: {address}\n"
                f"الإحداثيات: {lat:.4f}, {lon:.4f}\n\n"
                f"هل هذا العنوان صحيح؟",
        reply_markup=reply_markup
    )
    
    return USER_VEHICLE_TYPE

async def destination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ردود تأكيد الوجهة"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'edit_destination':
        await query.edit_message_caption(
            caption="✏️ يرجى إعادة كتابة الوجهة:"
        )
        return USER_DESTINATION
    
    # حساب المسافة والسعر
    pickup = context.user_data['pickup_location']
    destination = context.user_data['destination']
    
    # حساب المسافة (مبسط - في الإنتاج استخدم OSRM أو Google Maps)
    distance = await calculate_distance(
        pickup['lat'], pickup['lon'],
        destination['lat'], destination['lon']
    )
    
    # عرض خيارات المركبات
    keyboard = []
    for key, vehicle in VEHICLE_TYPES.items():
        price = distance * 0.5 * vehicle['price_multiplier']  # 0.5 وحدة لكل كم
        context.user_data[f'price_{key}'] = price
        
        keyboard.append([
            InlineKeyboardButton(
                f"{vehicle['name']} - {price:.2f} 💰", 
                callback_data=f'vehicle_{key}'
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_caption(
        caption=f"🚗 **اختر نوع المركبة**\n\n"
                f"المسافة التقريبية: {distance:.1f} كم\n"
                f"الوقت المتوقع: {distance*2:.0f} دقيقة\n\n"
                f"الأسعار تشمل العمولة والضرائب:",
        reply_markup=reply_markup
    )
    
    return USER_CONFIRM_RIDE

async def calculate_distance(lat1, lon1, lat2, lon2):
    """حساب المسافة بين نقطتين (Haversine formula)"""
    from math import radians, sin, cos, sqrt, atan2
    
    R = 6371.0  # نصف قطر الأرض بالكيلومتر
    
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    
    return R * c

async def vehicle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة اختيار نوع المركبة"""
    query = update.callback_query
    await query.answer()
    
    vehicle_key = query.data.replace('vehicle_', '')
    vehicle = VEHICLE_TYPES[vehicle_key]
    price = context.user_data[f'price_{vehicle_key}']
    
    context.user_data['selected_vehicle'] = vehicle_key
    context.user_data['final_price'] = price
    
    pickup = context.user_data['pickup_location']
    destination = context.user_data['destination']
    
    keyboard = [
        [
            InlineKeyboardButton("✅ تأكيد وطلب", callback_data='confirm_ride'),
            InlineKeyboardButton("❌ إلغاء", callback_data='cancel_ride')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_caption(
        caption=f"📋 **مراجعة الطلب**\n\n"
                f"📍 **من:** {pickup.get('address', 'الموقع الحالي')}\n"
                f"🏁 **إلى:** {destination['address']}\n"
                f"🚗 **نوع المركبة:** {vehicle['name']}\n"
                f"💰 **السعر:** {price:.2f} وحدة\n\n"
                f"هل تريد تأكيد الطلب؟",
        reply_markup=reply_markup
    )
    
    return USER_CONFIRM_RIDE

async def ride_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة تأكيد الطلب"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'cancel_ride':
        await query.edit_message_caption(
            caption="❌ **تم إلغاء الطلب**\n\n"
                    "يمكنك البدء بطلب جديد في أي وقت."
        )
        return ConversationHandler.END
    
    # إنشاء الطلب
    ride_data = {
        'user_id': update.effective_user.id,
        'user_name': update.effective_user.first_name,
        'pickup': context.user_data['pickup_location'],
        'destination': context.user_data['destination'],
        'vehicle_type': context.user_data['selected_vehicle'],
        'price': context.user_data['final_price'],
        'status': 'searching',
        'created_at': datetime.now()
    }
    
    ride_id = db.create_ride(ride_data)
    
    # البحث عن سائقين
    await query.edit_message_caption(
        caption="🔍 **جاري البحث عن سائقين قريبين...**\n\n"
                "سيتم إعلامك فور قبول أحد السائقين للطلب."
    )
    
    # محاكاة البحث عن سائق (في الإنتاج، أبحث في قاعدة البيانات عن سائقين قريبين)
    await asyncio.sleep(3)
    
    # محاكاة قبول السائق
    driver_id = find_nearby_driver(ride_data['pickup']['lat'], ride_data['pickup']['lon'])
    
    if driver_id:
        db.update_ride(ride_id, {
            'driver_id': driver_id,
            'status': 'accepted',
            'accepted_at': datetime.now()
        })
        
        driver = db.drivers[driver_id]
        
        # إرسال خريطة التتبع
        map_url = await MapService.get_route_map(
            ride_data['pickup']['lat'], ride_data['pickup']['lon'],
            ride_data['destination']['lat'], ride_data['destination']['lon']
        )
        
        await query.message.reply_photo(
            photo=map_url,
            caption=f"✅ **تم قبول طلبك!**\n\n"
                    f"👤 **السائق:** {driver['name']}\n"
                    f"🚗 **المركبة:** {VEHICLE_TYPES[ride_data['vehicle_type']]['name']}\n"
                    f"📱 **رقم الهاتف:** {driver.get('phone', 'غير متوفر')}\n"
                    f"⏱ **الوقت المتوقع:** 5-10 دقائق\n\n"
                    f"يمكنك تتبع المسار على الخريطة أعلاه."
        )
        
        # محاكاة وصول السائق
        await asyncio.sleep(5)
        
        await query.message.reply_text(
            "🚗 **وصل السائق إلى موقع الانطلاق وينتظرك!**\n\n"
            "يرجى التوجه إلى موقع الانطار."
        )
        
        # محاكاة بدء الرحلة
        await asyncio.sleep(10)
        
        db.update_ride(ride_id, {'status': 'started', 'started_at': datetime.now()})
        
        await query.message.reply_text(
            "🚀 **بدأت الرحلة!**\n\n"
            "يتم توجيهك الآن إلى الوجهة."
        )
        
        # محاكاة انتهاء الرحلة
        await asyncio.sleep(15)
        
        db.update_ride(ride_id, {'status': 'completed', 'completed_at': datetime.now()})
        
        # معالجة الدفع
        user = db.users[update.effective_user.id]
        user['balance'] -= ride_data['price']
        
        await query.message.reply_text(
            f"🏁 **وصلت إلى وجهتك!**\n\n"
            f"✅ **تم إنهاء الرحلة**\n"
            f"💰 **تم خصم:** {ride_data['price']:.2f} وحدة\n"
            f"💳 **الرصيد المتبقي:** {user['balance']:.2f} وحدة\n\n"
            f"شكراً لاستخدامك خدمتنا!"
        )
        
        # طلب التقييم
        keyboard = [
            [InlineKeyboardButton("⭐" * i, callback_data=f'rate_{i}') for i in range(1, 6)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(
            "🌟 **كيف كانت تجربتك؟**\n\n"
            "يرجى تقييم الرحلة من 1 إلى 5 نجوم:",
            reply_markup=reply_markup
        )
        
        return USER_RATING
    
    return USER_RATING

def find_nearby_driver(pickup_lat, pickup_lon):
    """البحث عن سائق قريب (محاكاة)"""
    for driver_id, driver in db.drivers.items():
        if driver.get('is_active', False):
            return driver_id
    return None

async def handle_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة التقييم"""
    query = update.callback_query
    await query.answer()
    
    rating = int(query.data.replace('rate_', ''))
    
    await query.edit_message_text(
        f"🌟 **شكراً لتقييمك!** ({'⭐' * rating})\n\n"
        "نحن نسعى دائماً لتقديم أفضل خدمة."
    )
    
    keyboard = [
        [KeyboardButton("🚖 طلب مشوار جديد")],
        [KeyboardButton("📋 سجل المشاوير"), KeyboardButton("👤 إدارة الحساب")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await query.message.reply_text(
        "اختر الخيار المطلوب:",
        reply_markup=reply_markup
    )
    
    return USER_START

# ============= دوال السائق =============
async def driver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء وضع السائق"""
    user = update.effective_user
    
    db.add_driver(user.id, {
        'name': user.first_name,
        'username': user.username,
        'is_active': False,
        'joined_at': datetime.now()
    })
    
    keyboard = [
        [KeyboardButton("🚗 تفعيل الاستلام")],
        [KeyboardButton("❌ إلغاء الاستلام")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"👋 أهلاً بك {user.first_name} في وضع السائق!\n\n"
        "يمكنك تفعيل وضع الاستلام لتلقي طلبات المشاوير.",
        reply_markup=reply_markup
    )
    
    return DRIVER_START

async def activate_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تفعيل وضع استلام الطلبات"""
    driver = db.drivers[update.effective_user.id]
    driver['is_active'] = True
    
    await update.message.reply_text(
        "✅ **تم تفعيل وضع الاستلام**\n\n"
        "ستصلك إشعارات بطلبات المشاوير القريبة من موقعك."
    )
    
    # محاكاة إرسال طلب جديد
    await asyncio.sleep(2)
    
    keyboard = [
        [
            InlineKeyboardButton("✅ قبول الطلب", callback_data='accept_ride_1'),
            InlineKeyboardButton("❌ رفض", callback_data='reject_ride_1')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🔔 **طلب جديد!**\n\n"
        "📍 **من:** ميدان التحرير\n"
        "🏁 **إلى:** مدينة نصر\n"
        "🚗 **النوع:** اقتصادي\n"
        "💰 **السعر:** 25.50 وحدة\n"
        "📏 **المسافة:** 8.2 كم\n\n"
        "هل تريد قبول هذا الطلب؟",
        reply_markup=reply_markup
    )
    
    return DRIVER_ACTIVE

async def deactivate_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء وضع الاستلام"""
    driver = db.drivers[update.effective_user.id]
    driver['is_active'] = False
    
    await update.message.reply_text(
        "❌ **تم إلغاء وضع الاستلام**\n\n"
        "لن تصلك أي طلبات جديدة حتى إعادة التفعيل."
    )
    
    return DRIVER_START

async def driver_ride_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ردود السائق على الطلبات"""
    query = update.callback_query
    await query.answer()
    
    if 'reject' in query.data:
        await query.edit_message_text("❌ **تم رفض الطلب**\n\nستصلك طلبات أخرى قريباً.")
        return DRIVER_ACTIVE
    
    # قبول الطلب
    await query.edit_message_text(
        "✅ **تم قبول الطلب بنجاح!**\n\n"
        "يرجى التوجه إلى موقع الانطار.\n\n"
        "خيارات:"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("🗺️ عرض المسار على الخريطة", callback_data='show_route'),
            InlineKeyboardButton("🚗 تم الانطلاق", callback_data='started_trip')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.reply_text(
        "اختر الإجراء التالي:",
        reply_markup=reply_markup
    )
    
    return DRIVER_ON_TRIP

# ============= الدالة الرئيسية =============
def main():
    """تشغيل البوت"""
    # احصل على التوكن من متغير البيئة
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8040839230:AAEtLPIAA8XyL36IbjBQfBu9lbkE447jJRY')
    
    if TOKEN == 'YOUR_BOT_TOKEN_HERE':
        print("⚠️  يرجى تعيين التوكن الخاص بالبوت في متغير البيئة TELEGRAM_BOT_TOKEN")
        return
    
    # إنشاء التطبيق
    application = Application.builder().token(TOKEN).build()
    
    # محادثة المستخدم
    user_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.Regex('^(طلب مشوار)$'), request_ride)
        ],
        states={
            USER_START: [
                MessageHandler(filters.Regex('^(طلب مشوار)$'), request_ride),
                # يمكن إضافة معالجات أخرى للخيارات الرئيسية
            ],
            USER_LOCATION: [
                MessageHandler(filters.LOCATION | filters.TEXT, handle_location),
                CallbackQueryHandler(location_callback, pattern='^(confirm_location|edit_location)$')
            ],
            USER_DESTINATION: [
                CallbackQueryHandler(location_callback, pattern='^(confirm_location|edit_location)$'),
                MessageHandler(filters.TEXT, handle_destination)
            ],
            USER_VEHICLE_TYPE: [
                CallbackQueryHandler(destination_callback, pattern='^(confirm_destination|edit_destination)$'),
                CallbackQueryHandler(vehicle_callback, pattern='^vehicle_')
            ],
            USER_CONFIRM_RIDE: [
                CallbackQueryHandler(ride_confirmation_callback, pattern='^(confirm_ride|cancel_ride)$')
            ],
            USER_RATING: [
                CallbackQueryHandler(handle_rating, pattern='^rate_[1-5]$')
            ]
        },
        fallbacks=[CommandHandler('start', start)],
        allow_reentry=True
    )
    
    # محادثة السائق
    driver_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('driver', driver_start)],
        states={
            DRIVER_START: [
                MessageHandler(filters.Regex('^(🚗 تفعيل الاستلام|تفعيل الاستلام)$'), activate_driver),
                MessageHandler(filters.Regex('^(❌ إلغاء الاستلام|إلغاء الاستلام)$'), deactivate_driver)
            ],
            DRIVER_ACTIVE: [
                CallbackQueryHandler(driver_ride_callback, pattern='^(accept_ride|reject_ride)')
            ],
            DRIVER_ON_TRIP: [
                CallbackQueryHandler(driver_ride_callback, pattern='^(show_route|started_trip)')
            ]
        },
        fallbacks=[CommandHandler('driver', driver_start)]
    )
    
    # إضافة المعالجات
    application.add_handler(user_conv_handler)
    application.add_handler(driver_conv_handler)
    
    # تشغيل البوت
    print("🤖 البوت يعمل الآن...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()